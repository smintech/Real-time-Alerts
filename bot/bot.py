import logging
import asyncio
from datetime import datetime, timedelta

from telegram.ext import Application, ContextTypes

from config import TELEGRAM_TOKEN
from bot.commands import get_application_handlers, user_watches  # renamed file
from bot.settings import (
    CHECK_INTERVAL_SECONDS,
    TIMEZONE,
    MAX_WATCH_FAILURES,
    FAILURE_BACKOFF_BASE,
    NOTIFY_ON_DELISTED,
)
from utils.utils import scrape_product, compute_changes, calculate_deal_score
from utils.format import format_telegram_alert  # renamed file

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

application: Application | None = None  # global if needed elsewhere


async def safe_send(bot, chat_id: int, text: str, **kwargs):
    """Safe message sender — logs but never raises."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except Exception as exc:
        LOG.exception("Failed sending to %s: %s", chat_id, exc)


async def handle_watch_failure(context: ContextTypes.DEFAULT_TYPE, user_id: int, watch: dict, exc=None, reason=None):
    """Centralized failure handling with backoff + pause."""
    watch.setdefault("fail_count", 0)
    watch["fail_count"] += 1

    backoff_seconds = FAILURE_BACKOFF_BASE * (2 ** (watch["fail_count"] - 1))
    watch["next_check"] = datetime.now(TIMEZONE) + timedelta(seconds=backoff_seconds)

    LOG.warning(
        "Watch failure user=%s url=%s count=%d backoff=%ds reason=%s",
        user_id, watch.get("url"), watch["fail_count"], backoff_seconds, reason or str(exc)[:100]
    )

    if watch["fail_count"] >= MAX_WATCH_FAILURES:
        watch["status"] = "paused"
        msg = (
            f"⚠️ Watch for *{watch.get('title', 'product')}* paused after repeated failures.\n\n"
            "Possible causes: product removed, site changes, or temporary issues.\n"
            "We'll resume automatically if it recovers, or remove/re-add."
        )
        await safe_send(context.bot, user_id, msg, parse_mode="Markdown")
        LOG.info("Paused watch user=%s url=%s after %d failures", user_id, watch.get("url"), watch["fail_count"])


async def check_all_watches(context: ContextTypes.DEFAULT_TYPE):
    """Job: check all active watches → alert on changes."""
    if not user_watches:
        return

    user_ids = list(user_watches.keys())
    now = datetime.now(TIMEZONE)

    for user_id in user_ids:
        watches = user_watches.get(user_id, [])
        for watch in list(watches):
            try:
                if watch.get("status") != "active":
                    continue

                next_check = watch.get("next_check")
                if next_check and now < next_check:
                    continue

                # Scrape
                try:
                    new_data = await asyncio.get_event_loop().run_in_executor(None, scrape_product, watch["url"])
                except Exception as exc:
                    await handle_watch_failure(context, user_id, watch, exc=exc, reason="scrape_error")
                    continue

                if not new_data or new_data.get("current_price") is None:
                    if NOTIFY_ON_DELISTED:
                        await safe_send(
                            context.bot,
                            user_id,
                            f"⚠️ *{watch.get('title', 'Product')}* delisted or OOS — pausing watch.\n{watch['url']}",
                            parse_mode="Markdown"
                        )
                    watch["status"] = "paused"
                    continue

                # Reset failures on success
                watch["fail_count"] = 0
                watch.pop("next_check", None)

                old_price = watch.get("last_price")
                current_price = new_data["current_price"]

                if old_price is None:
                    watch["last_price"] = current_price
                    continue

                changes = compute_changes(
                    {"current_price": old_price, "stock_status": "available"},
                    {"current_price": current_price, "stock_status": new_data.get("stock_status", "available")}
                )

                if not changes.get("significant_change"):
                    watch["last_price"] = current_price
                    continue

                price_diff_percent = changes["price_diff_percent"]  # positive = drop
                direction = watch.get("direction", "low")
                deal_score = calculate_deal_score(price_diff_percent)

                # Trigger logic
                trigger = False
                if direction == "low" and price_diff_percent > 0:
                    trigger = True
                elif direction == "high" and price_diff_percent < 0:
                    trigger = True
                elif direction == "both":
                    trigger = True

                # Target price
                target_price = watch.get("target_price")
                target_hit = False
                if target_price is not None and current_price <= target_price:
                    target_hit = True
                    trigger = True

                if not trigger:
                    watch["last_price"] = current_price
                    continue

                # Enriched alert data
                enriched = {
                    "title": new_data.get("title", watch.get("title", "Product")),
                    "current_price": current_price,
                    "previous_price": old_price,
                    "price_diff_percent": abs(price_diff_percent),
                    "deal_score": deal_score,
                    "suggested_action": (
                        f"Hit target ≤ ₦{int(target_price):,}" if target_hit
                        else "Price dropped!" if price_diff_percent > 0
                        else "Price increased!"
                    ),
                    "product_url": watch["url"],
                    "changed": True,
                    "what_changed": changes.get("what_changed", []),
                }

                msg = format_telegram_alert(enriched)

                await safe_send(
                    context.bot,
                    user_id,
                    msg,
                    parse_mode="Markdown",
                    disable_web_page_preview=True
                )

                watch["last_price"] = current_price

            except Exception as exc:
                LOG.exception("Unexpected scheduler error user=%s watch=%s", user_id, watch.get("url"))
                await handle_watch_failure(context, user_id, watch, exc=exc, reason="unexpected")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.exception("Unhandled handler error: %s", context.error)


def run_bot():
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    for handler in get_application_handlers():
        application.add_handler(handler)

    application.add_error_handler(global_error_handler)

    application.job_queue.run_repeating(
        callback=check_all_watches,
        interval=CHECK_INTERVAL_SECONDS,
        first=30,
        name="price_checker"
    )

    LOG.info("Bot + scheduler started")
    application.run_polling()