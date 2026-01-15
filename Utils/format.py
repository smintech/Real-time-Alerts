import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Any
from urllib.parse import urlparse
import request
# Attempt to import a timezone object from your settings; fall back to UTC if unavailable.
try:
    from bot.settings import TIMEZONE  # expected to be a tzinfo instance
    _TZ = TIMEZONE if hasattr(TIMEZONE, "tzname") else timezone.utc
except Exception:
    _TZ = timezone.utc

logger = logging.getLogger(__name__)
if not logger.handlers:
    # Basic logging config if the host app hasn't configured logging.
    logging.basicConfig(level=logging.INFO)

LIVE_USD_NGN_RATE = 1450.0

async def update_exchange_rate():
    global LIVE_USD_NGN_RATE
    try:
        # We fetch USDTNGN because it represents the actual street value of the Dollar in Naira
        url = "https://api.binance.com/api/v3/ticker/price?symbol=USDTNGN"
        response = requests.get(url, timeout=10)
        data = response.json()
        
        new_rate = float(data['price'])
        if new_rate > 500:  # Simple safety check
            LIVE_USD_NGN_RATE = new_rate
            print(f"✅ Exchange Rate Updated: ₦{LIVE_USD_NGN_RATE:,.2f}")
    except Exception as e:
        print(f"⚠️ Failed to update rate: {e}")

def _safe_currency(value: Any, site: str = "jumia") -> str:
    try:
        num = float(value)
        
        # Check if the product is from Binance/Crypto
        if "binance" in site.lower() or "crypto" in site.lower():
            naira_equivalent = num * LIVE_USD_NGN_RATE
            # Returns both: "$3,300 (₦4,785,000)"
            return f"${num:,.2f} (₦{naira_equivalent:,.0f})"
            
        # Default for Jumia/Konga
        return f"₦{int(num):,}"
    except:
        return "Price Unknown"

def _safe_percent(value: Any) -> float:
    """
    Return a float percent (0.0 default) from possibly missing/invalid input.
    """
    try:
        return float(value or 0.0)
    except Exception:
        return 0.0


def _safe_url(url: Optional[str]) -> str:
    """
    Do quick sanitization and validation of URL; return original if looks ok,
    otherwise return empty string.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return url
        # allow scheme-less by prepending https
        if parsed.path and not parsed.netloc:
            return "https://" + url.lstrip("/")
        return ""
    except Exception:
        return ""


def format_telegram_alert(enriched_data: Dict) -> str:
    """
    Format a clean, engaging Telegram alert with bold markdown.
    This function is resilient: if fields are missing it will include
    a gentle warning and still return a usable message.

    Returns a string (Markdown) suitable for Telegram send_message with parse_mode='Markdown'.
    """

    # Required-ish fields: title, current_price, product_url.
    title = enriched_data.get("title") or "Product"
    current_price_raw = enriched_data.get("current_price")
    previous_price_raw = enriched_data.get("previous_price")
    price_diff = _safe_percent(enriched_data.get("price_diff_percent"))
    deal_score_raw = (enriched_data.get("deal_score") or "UNKNOWN").upper()
    suggested_action = enriched_data.get("suggested_action") or "Check it out — prices move quickly!"
    product_url = _safe_url(enriched_data.get("product_url"))

    warnings = []

    # Validate numeric prices
    current_price = _safe_currency(current_price_raw)
    previous_price = _safe_currency(previous_price_raw) if previous_price_raw is not None else "unknown"

    if not product_url:
        warnings.append("URL missing or invalid")

    # Build emoji intensity by deal score
    try:
        score = deal_score_raw.upper()
        emoji_fire = "🔥" * (3 if score == "HIGH" else 2 if score == "MEDIUM" else 1)
    except Exception:
        emoji_fire = "🔥"

    # Price percent formatting
    try:
        percent_str = f"{price_diff:.1f}%"
    except Exception:
        percent_str = "0.0%"

    # If essential numeric values missing, add a warning to the message
    if current_price_raw is None:
        warnings.append("current price missing")
    # previous price might be deliberately absent

    # Compose message
    alert_lines = [
        f"🚨 *{title}* PRICE DROP ALERT!",
        "",
        f"💰 New price: *{current_price}* (was {previous_price})",
        f"📉 Drop: *{percent_str}*",
        f"Deal score: *{deal_score_raw}* {emoji_fire}",
        "",
        f"💡 {suggested_action}",
        "",
        f"🛒 Buy now: {product_url or 'Link unavailable'}"
    ]

    # Append warnings if any (non-fatal)
    if warnings:
        alert_lines.append("")
        alert_lines.append("⚠️ _Note:_ " + "; ".join(warnings))

    # If the dataset included an explicit error, show it concisely
    if err := enriched_data.get("error"):
        alert_lines.append("")
        alert_lines.append(f"❗ Error: {err}")

    return "\n".join(alert_lines)


def format_json_response(enriched_data: Dict, timestamp: Optional[str] = None) -> Dict:
    """
    Format enriched JSON for API responses or webhooks.
    Uses a timezone-safe timestamp. This function never raises on malformed input;
    instead it includes an 'error' or 'warning' key for predictable failure handling.
    """

    result: Dict[str, Any] = {}

    # Timestamp: if not provided, build one using fallback timezone
    try:
        if timestamp is None:
            timestamp = datetime.now(_TZ).isoformat()
    except Exception:
        # final fallback: UTC now
        timestamp = datetime.now(timezone.utc).isoformat()

    # Basic fields with safe fallbacks
    result["product_url"] = _safe_url(enriched_data.get("product_url")) or None
    result["title"] = enriched_data.get("title") or "Unknown Product"

    # Numeric conversions with safe defaults
    try:
        result["current_price"] = float(enriched_data["current_price"]) if "current_price" in enriched_data else None
    except Exception:
        # keep original if cast fails; caller can inspect 'warning' field
        result["current_price"] = None

    try:
        result["previous_price"] = float(enriched_data["previous_price"]) if "previous_price" in enriched_data else None
    except Exception:
        result["previous_price"] = None

    result["changed"] = bool(enriched_data.get("changed", False))
    result["what_changed"] = enriched_data.get("what_changed", []) or []
    result["price_diff_percent"] = round(_safe_percent(enriched_data.get("price_diff_percent")), 2)
    result["deal_score"] = (enriched_data.get("deal_score") or "UNKNOWN").upper()
    result["severity"] = result["deal_score"]  # alias for compatibility
    result["suggested_action"] = enriched_data.get("suggested_action", "Monitor closely")
    result["alternatives"] = enriched_data.get("alternatives", []) or []
    result["timestamp"] = timestamp

    # Add predictable failure details
    warnings = []
    if result["product_url"] is None:
        warnings.append("product_url missing or invalid")
    if result["current_price"] is None:
        warnings.append("current_price missing or non-numeric")
    if result["deal_score"] == "UNKNOWN":
        warnings.append("deal_score not provided")

    # If the source provided an explicit error, propagate it
    if "error" in enriched_data:
        result["error"] = enriched_data.get("error")

    if warnings:
        # include warnings array and a short summary
        result["warnings"] = warnings
        result["warning_summary"] = "; ".join(warnings)

    return result

def format_channel_deal(data: dict) -> str:
    title = data["title"]
    curr = f"₦{int(data['current_price']):,}"
    prev = f"₦{int(data['previous_price']):,}"
    drop_pct = data["price_diff_percent"]
    savings = int(data["previous_price"] - data["current_price"])
    score_text = {"high": "⭐⭐⭐ HIGH DEAL", "medium": "⭐⭐ MEDIUM DEAL", "low": "⭐ LOW"}.get(data["deal_score"], "")

    return f"""
🔥 HOT PRICE DROP ON JUMIA!

📱 {title}

💰 New: {curr}
👴 Was: {prev}
📉 Saved: ₦{savings:,} ({drop_pct}% off)

{score_text}

✅ In stock • Nationwide delivery

🛒 Grab it: {data['product_url']}

⏰ Spotted just now — prices change fast!

Personal alerts → @YourBotUsername
    """.strip()
