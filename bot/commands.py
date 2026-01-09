import asyncio
import logging
import time
import urllib.parse
from typing import List, Dict, Optional

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.settings import (
    MAX_WATCHES_FREE,
    ADD_COOLDOWN_SECONDS,
    SCRAPE_TIMEOUT,
    ALLOWED_DIRECTIONS,
)
from utils.utils import scrape_product

# -------------------------
# In-memory store (MVP – replace with DB soon)
# -------------------------
user_watches: Dict[int, List[Dict]] = {}
_user_last_action: Dict[int, float] = {}

# -------------------------
# Configuration (moved to settings.py)
# -------------------------
LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# -------------------------
# Helpers
# -------------------------
def is_valid_jumia_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        hostname = (parsed.hostname or "").lower()
        return "jumia" in hostname and parsed.path and parsed.path != "/"
    except Exception:
        return False


def user_cooldown_check(user_id: int) -> float:
    now = time.time()
    last = _user_last_action.get(user_id, 0)
    elapsed = now - last
    if elapsed >= ADD_COOLDOWN_SECONDS:
        return 0.0
    return ADD_COOLDOWN_SECONDS - elapsed


def safe_price_format(price: Optional[float]) -> str:
    if price is None:
        return "N/A"
    try:
        return f"₦{int(price):,}"
    except Exception:
        return f"₦{price}"


def parse_add_args(args: List[str]) -> tuple[str, Optional[int], str]:
    if not args:
        raise ValueError("No URL provided")

    url = args[0].strip()
    target_price: Optional[int] = None
    direction = "low"

    if len(args) > 1:
        second_arg = args[1].lower()
        cleaned = ''.join(filter(str.isdigit, second_arg))
        if cleaned:
            try:
                target_price = int(cleaned)
            except ValueError:
                pass
        elif second_arg in ALLOWED_DIRECTIONS:
            direction = second_arg

    if len(args) > 2:
        last_arg = args[-1].lower()
        if last_arg in ALLOWED_DIRECTIONS:
            direction = last_arg

    return url, target_price, direction


# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to Naija Price Alerts! (MVP)\n\n"
        "Track Jumia phones/gadgets. Get pings on price changes.\n\n"
        "Commands:\n"
        "/add <Jumia URL> [target_price] [low|high|both] — Add watch\n"
        "   • Default: alert on price DROPS only (free)\n"
        "   • target_price: alert when ≤ this amount\n"
        "   • high/both: premium (coming soon)\n"
        f"   • Max {MAX_WATCHES_FREE} watches free\n"
        "/list — View watches\n"
        "/remove <number> — Delete watch\n\n"
        "Example: /add https://www.jumia.com.ng/iphone-15 800000"
    )


async def add_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    try:
        url, target_price, direction = parse_add_args(context.args)
    except ValueError:
        await update.message.reply_text(
            "❌ Usage: /add <Jumia URL> [target_price] [low|high|both]\n"
            "Example: /add https://www.jumia.com.ng/iphone-15 800000"
        )
        return

    if not url.startswith(("http://", "https://")) or not is_valid_jumia_url(url):
        await update.message.reply_text("❌ Invalid Jumia product URL.")
        return

    cooldown_left = user_cooldown_check(user_id)
    if cooldown_left > 0:
        await update.message.reply_text(f"⏳ Chill — try again in {int(cooldown_left)}s.")
        return

    watches = user_watches.get(user_id, [])
    if len(watches) >= MAX_WATCHES_FREE:
        await update.message.reply_text(
            f"🚫 Free limit: {MAX_WATCHES_FREE} watches. Remove one or upgrade later."
        )
        return

    if direction in ["high", "both"]:
        await update.message.reply_text(
            "ℹ️ Price increase/any-change alerts are premium (coming soon).\n"
            "Defaulting to drops only."
        )
        direction = "low"

    _user_last_action[user_id] = time.time()

    try:
        loop = asyncio.get_running_loop()
        scrape_task = loop.run_in_executor(None, scrape_product, url)
        data = await asyncio.wait_for(scrape_task, timeout=SCRAPE_TIMEOUT)

        title = data.get("title") or "Unknown Product"
        current_price = data.get("current_price")

        if current_price is None:
            raise ValueError("Product out of stock or delisted")

        watch = {
            "url": url,
            "title": title,
            "last_price": current_price,
            "target_price": target_price,
            "direction": direction,
            "added_at": int(time.time()),
            "status": "active"
        }
        watches.append(watch)
        user_watches[user_id] = watches

        msg = f"✅ Watch added: {title}\nCurrent: {safe_price_format(current_price)}"
        if target_price:
            msg += f"\nTarget: {safe_price_format(target_price)} (alert when ≤)"
        msg += f"\nMode: drops only"
        await update.message.reply_text(msg)

    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ Validation timed out — Jumia slow. Try again soon.")
    except ValueError as ve:
        await update.message.reply_text(f"❌ Invalid product: {str(ve)}")
    except Exception as exc:
        LOG.exception("Add failed for %s: %s", user_id, exc)
        await update.message.reply_text(
            "❌ Could not validate product (possible 404/block).\n"
            "Check URL and try again in a minute."
        )


async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    watches = user_watches.get(user_id, [])
    if not watches:
        await update.message.reply_text("📭 No watches. Add with /add <URL>")
        return

    lines = ["*Your Active Watches:*\n"]
    for i, w in enumerate(watches, 1):
        title = w.get("title", "Unknown")
        url = w.get("url", "")
        price = safe_price_format(w.get("last_price"))
        target = f" | Target ≤ {safe_price_format(w.get('target_price'))}" if w.get("target_price") else ""
        mode_text = {"low": "drops only", "high": "increases", "both": "any change"}.get(w.get("direction", "low"), "drops only")
        lines.append(
            f"{i}. {title}{target}\n"
            f"   Current: {price} | Mode: {mode_text}\n"
            f"   {url}"
        )

    message = "\n\n".join(lines)
    for i in range(0, len(message), 3500):
        await update.message.reply_text(message[i:i+3500], parse_mode="Markdown")


async def remove_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /remove <number> (see /list)")
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Provide a valid number.")
        return

    watches = user_watches.get(user_id, [])
    if not (0 <= idx < len(watches)):
        await update.message.reply_text("❌ Invalid number. Use /list.")
        return

    removed = watches.pop(idx)
    user_watches[user_id] = watches
    await update.message.reply_text(f"🗑️ Removed: {removed.get('title', 'Watch')}")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.exception("Unhandled error: %s", update)
    if update and hasattr(update, "message") and update.message:
        await update.message.reply_text(
            "⚠️ Something went wrong. Logged & will be fixed. Try again soon."
        )


def get_application_handlers():
    return [
        CommandHandler("start", start),
        CommandHandler("add", add_watch),
        CommandHandler("list", list_watches),
        CommandHandler("remove", remove_watch),
    ]