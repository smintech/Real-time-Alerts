"""
Fuel (Fuel Price Watch) and LPG price scrapers.
"""
import asyncio
import logging
import re
import json
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, Any, Optional, Tuple, List
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from .browser import fetch_with_playwright_aggressive, shared_playwright
from .helpers import get_domain_from_url

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
# Helper functions (naira formatting, block detection)
# -------------------------------------------------------------------
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

# -------------------------------------------------------------------
# Fuel Price Watch analyzer / parser
# -------------------------------------------------------------------
def analyze_fuel_html(html: str, url: str = "https://app.fuelpricewatch.com/") -> Dict[str, Any]:
    """Logs a human‑friendly analysis of the fuel price page. Returns dict with analysis."""
    LOG.info("=" * 60)
    LOG.info("⛽ FUEL PRICE WATCH ANALYZER")
    LOG.info("=" * 60)
    LOG.info("Date: %s", datetime.utcnow().strftime("%Y-%m-%d"))
    LOG.info("Target: %s", url)
    LOG.info("=" * 60)
    soup = BeautifulSoup(html or "", "lxml")
    page_text = soup.get_text(" ", strip=True)
    total_elements = len(soup.find_all(True))
    LOG.info("📜 Scanning page content...")
    LOG.info("============================================================")
    LOG.info("📊 ANALYSIS REPORT")
    LOG.info("============================================================")
    LOG.info("📈 Total elements analyzed: %d", total_elements)
    page_title = soup.title.string.strip() if soup.title and soup.title.string else "No title"
    LOG.info("🌐 Page title: %s", page_title)
    LOG.info("-" * 60)
    LOG.info("💰 PRICE ELEMENTS FOUND:")
    LOG.info("-" * 60)
    price_elems = []
    for tag in soup.find_all(['div', 'span', 'p', 'li', 'td']):
        txt = (tag.get_text(" ", strip=True) or "")
        if '₦' in txt or re.search(r'\bNGN\b', txt, re.I) or re.search(r'\bN\s*\d', txt):
            price_elems.append(tag)
    LOG.info("💰 Price elements found: %d", len(price_elems))
    for i, elem in enumerate(price_elems, start=1):
        full_text = elem.text
        outer_html = str(elem)
        preview = outer_html if len(outer_html) <= 1000 else outer_html[:1000] + " ...[truncated]..."
        classes = elem.get("class", [])
        classes_str = " ".join(classes) if classes else "(no classes)"
        LOG.info("%d. [%s] Full text:", i, elem.name)
        LOG.info("   %s", full_text or "(empty)")
        LOG.info("   Classes: %s", classes_str)
        LOG.info("   Outer HTML preview (first 1000 chars):")
        LOG.info("   %s", preview)
        LOG.info("-" * 40)
    LOG.info("-" * 60)
    LOG.info("📈 '+₦... today' ELEMENTS FOUND:")
    LOG.info("-" * 60)
    change_pattern = re.compile(r'([+-]\s*₦\s*[0-9\.,]+)\s*today', re.I)
    plus_today_elems = []
    for tag in soup.find_all(['div', 'span', 'p', 'section']):
        txt = (tag.get_text(" ", strip=True) or "")
        if change_pattern.search(txt):
            plus_today_elems.append(tag)
    LOG.info("📈 '+₦... today' elements found: %d", len(plus_today_elems))
    for i, elem in enumerate(plus_today_elems, start=1):
        full_text = elem.get_text(" ", strip=True)
        match = change_pattern.search(full_text)
        matched_text = match.group(0) if match else "(no exact match captured)"
        outer_html = str(elem)
        preview = outer_html if len(outer_html) <= 1200 else outer_html[:1200] + " ...[truncated]..."
        classes = elem.get("class", [])
        classes_str = " ".join(classes) if classes else "(no classes)"
        LOG.info("%d. [%s] Matched: %s", i, elem.name, matched_text)
        LOG.info("   Full text: %s", full_text)
        LOG.info("   Classes: %s", classes_str)
        LOG.info("   Outer HTML preview (first 1200 chars):")
        LOG.info("   %s", preview)
        LOG.info("-" * 40)
    low_matches = re.findall(r'\bLow[:\s]*₦?\s*([0-9,\.]+)', page_text, re.I)
    high_matches = re.findall(r'\bHigh[:\s]*₦?\s*([0-9,\.]+)', page_text, re.I)
    if low_matches:
        LOG.info("Found 'Low:' values (sample): %s", low_matches[:5])
    if high_matches:
        LOG.info("Found 'High:' values (sample): %s", high_matches[:5])
    perc_patterns = [
        r'([+-]?\s*[0-9]+\.?[0-9]*\s*%)\s*from last period',
        r'([+-]?\s*[0-9]+\.?[0-9]*\s*%)'
    ]
    perc_found = []
    for pat in perc_patterns:
        perc_found.extend(re.findall(pat, page_text, re.I)[:5])
    if perc_found:
        LOG.info("✅ FOUND: Percentage change examples - %s", perc_found[:5])
    else:
        LOG.warning("❌ NOT FOUND: No immediate percentage-change patterns")
    abs_patterns = [
        r'([+-]?\s*₦\s*[0-9\.,]+)\s*today',
        r'today[^\d₦]*([+-]?\s*₦\s*[0-9\.,]+)'
    ]
    abs_found = []
    for pat in abs_patterns:
        abs_found.extend(re.findall(pat, page_text, re.I)[:5])
    if abs_found:
        LOG.info("✅ FOUND: Absolute change examples - %s", abs_found[:5])
    else:
        LOG.warning("❌ NOT FOUND: No '₦X today' absolute change patterns")
    LOG.info("\n" + "─" * 60)
    LOG.info("📊 ACTUAL CSS CLASSES IN PAGE (Top 15)")
    LOG.info("─" * 60)
    all_classes = []
    for elem in soup.find_all(True)[:2000]:
        cls = elem.get("class", [])
        if cls:
            all_classes.extend(cls)
    class_counter = Counter(all_classes)
    top_classes = class_counter.most_common(15)
    for cls, cnt in top_classes:
        LOG.info("   %s: %dx", cls, cnt)
    LOG.info("\n" + "-" * 60)
    LOG.info("💾 Note: This run does NOT save CSV or screenshot files.")
    LOG.info("============================================================")
    LOG.info("🔧 NEXT STEPS YOU COULD TRY:")
    LOG.info("1. Click the 'Driver' button to access station search (if interactive).")
    LOG.info("2. Enter a location to see actual station prices.")
    LOG.info("3. Review 'Next update in:' timers on the page.")
    LOG.info("")
    LOG.info("🎯 Analysis complete!")
    result = {
        "total_elements": total_elements,
        "page_title": page_title,
        "price_elements_count": len(price_elems),
        "price_elements": [
            {
                "tag": e.name,
                "full_text": e.get_text(" ", strip=True),
                "classes": e.get("class", []),
                "outer_html_preview": (str(e)[:1200] + '...[truncated]') if len(str(e)) > 1200 else str(e)
            } for e in price_elems
        ],
        "plus_today_count": len(plus_today_elems),
        "plus_today_elements": [
            {
                "tag": e.name,
                "matched_text": (change_pattern.search(e.get_text(" ", strip=True)).group(0)
                                 if change_pattern.search(e.get_text(" ", strip=True)) else None),
                "full_text": e.get_text(" ", strip=True),
                "classes": e.get("class", []),
                "outer_html_preview": (str(e)[:1600] + '...[truncated]') if len(str(e)) > 1600 else str(e)
            } for e in plus_today_elems
        ],
        "top_css_classes": [c for c, _ in top_classes],
        "percent_examples": perc_found,
        "absolute_change_examples": abs_found,
    }
    return {"analysis": result}

@dataclass
class FoundElement:
    """Structured logging for found elements"""
    tag: str
    classes: str
    element_id: str
    text_preview: str
    price_found: Optional[str] = None
    change_found: Optional[str] = None
    def __str__(self) -> str:
        parts = [
            f"[{self.tag}]",
            f"Classes: {self.classes or 'none'}",
            f"ID: {self.element_id or 'none'}",
            f"Text: '{self.text_preview[:100]}...'"
        ]
        if self.price_found:
            parts.append(f"💰 Price: {self.price_found}")
        if self.change_found:
            parts.append(f"📈 Change: {self.change_found}")
        return " | ".join(parts)

def _parse_fuelpricewatch(html: str, url: str = "https://app.fuelpricewatch.com/") -> Dict[str, Any]:
    start_time = time.time()
    parse_session_id = f"parse_{int(start_time)}"
    LOG.info(f"\n{'='*80}")
    LOG.info(f"🔷 FUEL PRICE PARSE SESSION: {parse_session_id}")
    LOG.info(f"{'='*80}\n")
    # Normalise input
    if isinstance(html, (bytes, bytearray)):
        try:
            html_text = html.decode("utf-8", errors="replace")
        except Exception:
            html_text = html.decode("latin-1", errors="replace")
    else:
        html_text = str(html)
    html_text_normalized = html_text.replace("&#8358;", "₦").replace("&num;", "#")
    soup = BeautifulSoup(html_text_normalized, "html.parser")
    found_elements: List[FoundElement] = []
    price_raw = None
    change_percent = None
    change_absolute = None
    petrol_card = None
    extraction_log = []
    matched_card_contents = []
    # ---- Card discovery ----
    LOG.info("🔍 STEP 1: Finding Petrol Price Card (robust search)")
    LOG.info("  ├─ Strategy 1A: Searching for gumroad-card elements...")
    cards = soup.find_all("div", class_="gumroad-card")
    LOG.info(f"  │  └─ Found {len(cards)} gumroad-card elements")
    candidates = []
    for idx, card in enumerate(cards):
        card_text = card.get_text(" ", strip=True)
        card_html = str(card)
        if any(k in card_text for k in ("Petrol", "Fuel", "Diesel", "Kerosene")) or "₦" in card_html:
            candidates.append((idx, card, card_text, card_html))
    fallback_card_selectors = ["card", "price-card", "price", "stat", "grid", "rounded-lg", "bg-card"]
    for sel in fallback_card_selectors:
        found = soup.find_all(True, class_=re.compile(sel))
        for el in found:
            txt = el.get_text(" ", strip=True)
            if any(k in txt for k in ("Petrol", "Average Petrol")) or "₦" in txt:
                candidates.append(("fallback", el, txt, str(el)))
    seen_texts = set()
    filtered_candidates = []
    for item in candidates:
        txt = item[2]
        if txt not in seen_texts:
            seen_texts.add(txt)
            filtered_candidates.append(item)
    LOG.info(f"  ├─ Candidate cards after heuristic filtering: {len(filtered_candidates)}")
    for idx, card, card_text, card_html in filtered_candidates:
        if "Petrol" in card_text and ("₦" in card_text or re.search(r'\d{3,4}\.\d{2}', card_text)):
            petrol_card = card
            LOG.info("  │  ✓ Selected a candidate card containing 'Petrol' and price-like text")
            log_preview = card_html if len(card_html) <= 10000 else card_html[:10000] + "\n...<truncated>..."
            LOG.debug(f"  │    └─ Full card HTML (preview/truncated):\n{log_preview}\n")
            matched_card_contents.append(card_html)
            extraction_log.append("✓ Found petrol card via candidate heuristics (full card HTML logged)")
            break
    if not petrol_card:
        LOG.info("  ├─ Strategy 1B: Searching for 'Average Petrol' text anywhere in the page...")
        avg_nodes = soup.find_all(string=re.compile(r'Average\s+Petrol', re.I))
        if avg_nodes:
            petrol_card = avg_nodes[0].find_parent() or avg_nodes[0]
            LOG.info("  │  ✓ Found node containing 'Average Petrol' - logging full parent HTML")
            card_html = str(petrol_card)
            LOG.debug(f"  │    └─ Full card HTML (preview/truncated):\n{card_html[:10000]}...\n")
            matched_card_contents.append(card_html)
            extraction_log.append("✓ Found petrol card via 'Average Petrol' global search")
    if not petrol_card:
        LOG.info("  └─ ✗ No card-like element reliably identified (will attempt script/global fallback)\n")
    # ---- Price extraction ----
    def parse_number_str(num_str: str) -> float:
        try:
            return float(num_str.replace(",", "").strip())
        except Exception:
            m = re.search(r'(\d+(?:\.\d+)?)', num_str.replace(",", ""))
            return float(m.group(1)) if m else None
    def find_price_in_text(text: str):
        patterns = [
            (r'₦\s*([\d,]+(?:\.\d+)?)', "₦ with digits"),
            (r'&#8358;\s*([\d,]+(?:\.\d+)?)', "HTML entity &#8358;"),
            (r'\bNGN[:\s]*([\d,]+(?:\.\d+)?)\b', "NGN textual"),
            (r'\bNaira[:\s]*([\d,]+(?:\.\d+)?)\b', "Naira textual"),
            (r'Average\s+Petrol\s+Price[:\s]*₦?\s*([\d,]+(?:\.\d+)?)', "Average Petrol Price context"),
            (r'\b(\d{3,4}(?:,\d{3})*(?:\.\d+)?)\b(?=\s*(?:today|from last|from previous|from last period|from last))', "number followed by context words"),
        ]
        for pat, desc in patterns:
            m = re.search(pat, text, flags=re.IGNORECASE)
            if m:
                num = m.group(1)
                try:
                    val = parse_number_str(num)
                    LOG.info(f"    └─ ✓ Found price via {desc}: {val}")
                    return val, m.group(0), desc
                except Exception as e:
                    LOG.debug(f"    │  ✗ parse error for '{num}': {e}")
                    continue
        return None, None, None
    if petrol_card:
        LOG.info("💰 STEP 2: Extracting Price Data from Card (detailed)")
        full_text = petrol_card.get_text(" ", strip=True)
        card_html_str = str(petrol_card)
        LOG.info(f"  Card text length: {len(full_text)} characters")
        LOG.info(f"  Card HTML length: {len(card_html_str)} bytes")
        LOG.info(f"  Card text preview: {full_text[:200]}...\n")
        extraction_log.append("CARD_HTML:" + (card_html_str if len(card_html_str) < 20000 else card_html_str[:20000] + "...<truncated>"))
        price_val, matched_text, pattern_desc = find_price_in_text(full_text)
        if price_val is not None:
            price_raw = price_val
            extraction_log.append(f"✓ Price extracted from card: ₦{price_raw:.2f} (via {pattern_desc} / matched '{matched_text}')")
        else:
            LOG.warning("    └─ ✗ No price found inside selected card; will try card HTML and global fallbacks")
            extraction_log.append("✗ Price not found in selected card text")
        pct_match = re.search(r'([+\-]\s*\d+(?:\.\d+)?)\s*%', full_text)
        if pct_match:
            try:
                change_percent = float(pct_match.group(1).replace(" ", ""))
                LOG.info(f"    └─ ✓ Percent found in card: {change_percent}%")
                extraction_log.append(f"✓ Percent change (card): {change_percent}%")
            except Exception as e:
                LOG.debug(f"    │  ✗ Percent parse error: {e}")
        abs_match = re.search(r'([+\-]\s*₦\s*[\d,]+(?:\.\d+)?)|₦\s*([+\-]\s*[\d,]+(?:\.\d+)?)|([+\-]\s*[\d,]+(?:\.\d+)?)\s*today', full_text)
        if abs_match:
            candidate = next((g for g in abs_match.groups() if g), None)
            if candidate:
                change_absolute = candidate.replace(" ", "")
                LOG.info(f"    └─ ✓ Absolute change found in card: {change_absolute}")
                extraction_log.append(f"✓ Absolute change (card): {change_absolute}")
    if price_raw is None:
        LOG.warning("⚠️  STEP 3: Card extraction failed or incomplete; attempting page-level and <script> search")
        page_text = soup.get_text(" ", strip=True)
        LOG.info(f"  Page text length: {len(page_text):,} characters")
        LOG.debug(f"  Page text preview: {page_text[:500]}...\n")
        price_val, matched_text, pattern_desc = find_price_in_text(page_text)
        if price_val is not None:
            price_raw = price_val
            extraction_log.append(f"✓ Price extracted from page text: ₦{price_raw:.2f} (via {pattern_desc})")
            LOG.info(f"  ✓ Page-level price found: ₦{price_raw:.2f}")
        else:
            LOG.info("  ├─ ✗ No price in page text. Searching <script> tags and raw HTML...")
            script_search_hits = []
            for i, script in enumerate(soup.find_all("script")):
                s_text = script.string if script.string is not None else script.get_text(" ", strip=True)
                if not s_text:
                    continue
                if "₦" in s_text or "&#8358;" in s_text or "NGN" in s_text or "Naira" in s_text:
                    LOG.debug(f"    ├─ Script #{i+1} contains naira-like tokens; scanning for prices")
                    price_val, matched_text, pattern_desc = find_price_in_text(s_text)
                    script_preview = s_text if len(s_text) <= 8000 else s_text[:8000] + "...<truncated>..."
                    extraction_log.append("SCRIPT_HTML_PREVIEW:" + script_preview)
                    if price_val is not None:
                        script_search_hits.append((i, price_val, matched_text, pattern_desc, script_preview))
                        LOG.info(f"    └─ ✓ Found price in script #{i+1}: {price_val} (via {pattern_desc})")
                        break
            if script_search_hits and price_raw is None:
                price_raw = script_search_hits[0][1]
                extraction_log.append(f"✓ Price extracted from script: ₦{price_raw:.2f} (script #{script_search_hits[0][0]})")
            if price_raw is None:
                LOG.info("  ├─ Final fallback: scanning raw HTML for '₦' / '&#8358;' / NGN / Naira patterns")
                price_val, matched_text, pattern_desc = find_price_in_text(html_text_normalized)
                if price_val is not None:
                    price_raw = price_val
                    extraction_log.append(f"✓ Price extracted from raw HTML: ₦{price_raw:.2f} (via {pattern_desc})")
                    LOG.info(f"  ✓ Raw HTML price found: ₦{price_raw:.2f}")
                else:
                    LOG.warning("  └─ ✗ All global fallbacks failed to find a price\n")
                    extraction_log.append("✗ Global fallbacks failed")
    # ---- Diagnostic collection ----
    LOG.info("🔬 STEP 4: Collecting Diagnostic Information")
    LOG.info("  Scanning all elements for ₦ character or price indicators...")
    element_count = 0
    price_element_count = 0
    for elem in soup.find_all(['div', 'span', 'p', 'h1', 'h2', 'h3', 'li', 'td']):
        element_count += 1
        text = elem.get_text(" ", strip=True)
        has_naira = '₦' in text or '&#8358;' in str(elem)
        has_price_pattern = bool(re.search(r'\d{3,4}(?:,\d{3})*(?:\.\d{2})', text))
        has_fuel_type = any(f in text for f in ['Petrol', 'Diesel', 'Kerosene', 'Fuel', 'Average Petrol'])
        should_collect = (has_naira or (has_price_pattern and has_fuel_type)) and len(text) < 1000
        if should_collect:
            price_element_count += 1
            price_match = re.search(r'(₦\s*[\d,.]+|&#8358;\s*[\d,.]+|NGN[:\s]*[\d,.]+|Naira[:\s]*[\d,.]+)', text, flags=re.I)
            found_elements.append(FoundElement(
                tag=elem.name,
                classes=" ".join(elem.get("class", [])) if elem.get("class") else "",
                element_id=elem.get("id", ""),
                text_preview=text[:200],
                price_found=price_match.group(0) if price_match else None
            ))
    LOG.info(f"  ├─ Total elements scanned: {element_count:,}")
    LOG.info(f"  ├─ Price elements found: {price_element_count}")
    LOG.info(f"  └─ Elements with ₦: {sum(1 for e in found_elements if e.price_found)}\n")
    if found_elements:
        LOG.info("  📍 Found Elements (first 10):")
        for idx, elem in enumerate(found_elements[:10], 1):
            LOG.info(f"    {idx}. {elem}")
        if len(found_elements) > 10:
            LOG.info(f"    ... and {len(found_elements) - 10} more")
    else:
        LOG.warning("  ⚠️  No elements with ₦ found!\n")
    elapsed = time.time() - start_time
    success = price_raw is not None
    LOG.info("📊 FINAL SUMMARY")
    LOG.info(f"{'='*80}")
    LOG.info(f"  Extraction Success: {'✓ YES' if success else '✗ NO'}")
    LOG.info(f"  Price: {f'₦{price_raw:,.2f}' if price_raw else 'NOT FOUND'}")
    LOG.info(f"  Percent Change: {f'{change_percent}%' if change_percent is not None else 'NOT FOUND'}")
    LOG.info(f"  Absolute Change: {change_absolute or 'NOT FOUND'}")
    LOG.info(f"  Execution Time: {elapsed:.2f}s")
    LOG.info(f"{'='*80}\n")
    LOG.info("📋 EXTRACTION LOG:")
    for idx, entry in enumerate(extraction_log, 1):
        LOG.info(f"  {idx}. {entry if len(entry) < 400 else entry[:400] + '...'}")
    return {
        "source": url,
        "price_raw": float(price_raw) if price_raw is not None else 0.0,
        "price_str": f"₦{price_raw:,.2f}" if price_raw is not None else None,
        "change_percent": change_percent,
        "change_absolute": change_absolute,
        "last_updated": "Live data",
        "success": success,
        "diagnostic_elements": [str(e) for e in found_elements[:10]],
        "matched_card_contents": matched_card_contents,
        "parser_version": "2026.02.11-comprehensive-logging-v2",
        "execution_time_seconds": elapsed,
        "extraction_log": extraction_log,
    }

async def scrape_fuel_prices() -> Dict[str, Any]:
    """Scrape current fuel prices from Fuel Price Watch with comprehensive logging."""
    app_url = "https://app.fuelpricewatch.com/"
    LOG.info("\n" + "="*80)
    LOG.info("🚀 FUEL PRICE SCRAPER - SESSION START")
    LOG.info("="*80 + "\n")
    session_start = time.time()
    try:
        LOG.info("📡 Method 1: Fetching live app with Playwright\n")
        fetch_start = time.time()
        html = await fetch_with_playwright_aggressive(
            app_url,
            retries=3,
            return_visible_text=False,
            wait_for_selector="""div.gumroad-card:has-text('\u20A6'), div[class*="bg-card"]""",
            wait_timeout=30000
        )
        fetch_time = time.time() - fetch_start
        LOG.info(f"  ✓ Playwright fetch successful")
        LOG.info(f"    ├─ HTML size: {len(html):,} bytes")
        LOG.info(f"    └─ Fetch time: {fetch_time:.2f}s\n")
        parse_start = time.time()
        result = _parse_fuelpricewatch(html, url=app_url)
        parse_time = time.time() - parse_start
        LOG.info(f"\n  Parse time: {parse_time:.2f}s\n")
        if result.get("success"):
            LOG.info("✅ OVERALL STATUS: SUCCESS\n")
            return {
                "avg_petrol": result["price_str"],
                "avg_raw": result["price_raw"],
                "change_percent": result.get("change_percent", "N/A"),
                "change_absolute": result.get("change_absolute", "N/A"),
                "last_updated": result.get("last_updated", "Live"),
                "sources": [app_url],
                "debug": {
                    "method": "live_app_playwright",
                    "diagnostic_elements": result.get("diagnostic_elements", []),
                    "extraction_log": result.get("extraction_log", []),
                    "fetch_time_seconds": fetch_time,
                    "parse_time_seconds": parse_time,
                    "total_time_seconds": time.time() - session_start,
                }
            }
        else:
            LOG.error("❌ OVERALL STATUS: PARSING FAILED\n")
            return {
                "avg_petrol": "N/A",
                "avg_raw": None,
                "error": "parsing_failed",
                "debug": {
                    "diagnostic_elements": result.get("diagnostic_elements", []),
                    "extraction_log": result.get("extraction_log", []),
                    "parser_version": result.get("parser_version"),
                    "fetch_time_seconds": fetch_time,
                    "total_time_seconds": time.time() - session_start,
                }
            }
    except Exception as e:
        elapsed = time.time() - session_start
        LOG.exception(f"❌ EXCEPTION OCCURRED: {type(e).__name__}")
        return {
            "avg_petrol": "N/A",
            "avg_raw": None,
            "error": "exception",
            "exception": str(e),
            "debug": {"total_time_seconds": elapsed},
        }

# -------------------------------------------------------------------
# LPG scraper
# -------------------------------------------------------------------
@retry(max_attempts=3, backoff=1.5)
async def _fetch_lpg_html() -> str:
    url = "https://lpginnigeria.com/chart"
    return await fetch_with_playwright_aggressive(
        url,
        retries=3,
        return_visible_text=False,
        wait_for_selector='table.table-striped',
        wait_timeout=30000
    )

def _parse_lpg_price_string(price_str: Any) -> Optional[float]:
    if price_str is None:
        return None
    s = str(price_str).strip()
    if not s or s.upper() in ('N/A', 'NULL', 'NONE', '-'):
        return None
    s = re.sub(r'[^\d,.-]', '', s)
    if not s:
        return None
    try:
        is_negative = s.startswith('-')
        if is_negative:
            s = s[1:]
        s = s.replace(',', '')
        s = s.replace('.', '')
        value = float(s)
        return -value if is_negative else value
    except ValueError:
        match = re.search(r'([-]?[\d,]+(?:\.\d+)?)', str(price_str))
        if match:
            num_str = match.group(1).replace(',', '').replace('.', '')
            try:
                return float(num_str)
            except ValueError:
                return None
    return None

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
                price = _parse_lpg_price_string(price_raw) or 0.0
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
            price = _parse_lpg_price_string(price_str) or 0.0
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