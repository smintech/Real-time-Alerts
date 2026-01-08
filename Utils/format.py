from datetime import datetime
from typing import Dict, Optional

# New: Import timezone
from bot.settings import TIMEZONE

def format_telegram_alert(enriched_data: Dict) -> str:
    """
    Format a clean, engaging Telegram alert with bold markdown.
    """
    title = enriched_data['title']
    current = f"₦{enriched_data['current_price']:,}"
    previous = f"₦{enriched_data['previous_price']:,}" if enriched_data.get('previous_price') else "unknown"
    drop_percent = enriched_data.get('price_diff_percent', 0)
    deal_score = enriched_data['deal_score'].upper()
    action = enriched_data.get('suggested_action', 'Check it out fast — prices move quickly!')
    url = enriched_data['product_url']

    emoji_fire = "🔥" * (3 if deal_score == "HIGH" else 2 if deal_score == "MEDIUM" else 1)

    alert = (
        f"🚨 *{title}* PRICE DROP ALERT!\n\n"
        f"💰 New price: *{current}* (was {previous})\n"
        f"📉 Drop: *{drop_percent:.1f}%*\n"
        f"Deal score: *{deal_score}* {emoji_fire}\n\n"
        f"💡 {action}\n\n"
        f"🛒 Buy now: {url}"
    )
    return alert


def format_json_response(enriched_data: Dict, timestamp: Optional[str] = None) -> Dict:
    """
    Format enriched JSON for API responses or webhooks.
    Uses Nigeria timezone for timestamp.
    """
    if timestamp is None:
        timestamp = datetime.now(TIMEZONE).isoformat()

    return {
        "product_url": enriched_data['product_url'],
        "title": enriched_data['title'],
        "current_price": enriched_data['current_price'],
        "previous_price": enriched_data.get('previous_price'),
        "changed": enriched_data['changed'],
        "what_changed": enriched_data.get('what_changed', []),
        "price_diff_percent": round(enriched_data.get('price_diff_percent', 0), 2),
        "deal_score": enriched_data['deal_score'],
        "severity": enriched_data['deal_score'],  # alias; enhance later
        "suggested_action": enriched_data.get('suggested_action', 'Monitor closely'),
        "alternatives": enriched_data.get('alternatives', []),
        "timestamp": timestamp
    }