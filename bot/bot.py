# bot/bot.py
import logging
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, Any

from telegram.ext import Application, ContextTypes

from config import TELEGRAM_TOKEN
from bot.commands import get_application_handlers, user_watches, user_settings, user_subscriptions
from bot.settings import (
    CHECK_INTERVAL_SECONDS,
    TIMEZONE,
    MAX_WATCH_FAILURES,
    FAILURE_BACKOFF_BASE,
    NOTIFY_ON_DELISTED,
    CHANNEL_MONITORED_URLS,
    AUTO_POST_TO_CHANNEL,
    CHANNEL_DEAL_CHAT_ID,
    MAX_CHANNEL_POSTS_PER_RUN,
    MIN_DROP_PERCENT_FOR_CHANNEL,
    MIN_SAVINGS_FOR_CHANNEL,
    CATEGORIES,
    MAX_WATCHES_FREE,
    PAID_TIERS,
)

from utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    scrape_site,   # used by channel checker (if implemented)
    _get_domain_from_url  # helper from utils (if present)
)
from utils.format import format_telegram_alert, _safe_currency

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

application: Application | None = None  # global if needed elsewhere

# Channel caches (in-memory). Consider Redis/DB for persistence later.
channel_price_cache: Dict[str, float] = {}
channel_posted_today = set()
last_cache_date = None


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
            "We'll resume automatically if it recovers, or you can re-add it."
        )
        try:
            await safe_send(context.bot, user_id, msg, parse_mode="Markdown")
        except Exception:
            LOG.exception("Failed to notify user about paused watch")
        LOG.info("Paused watch user=%s url=%s after %d failures", user_id, watch.get("url"), watch["fail_count"])


async def check_all_watches(context: ContextTypes.DEFAULT_TYPE):
    """Job: check all active watches → alert on changes."""
    if not user_watches:
        return

    now = datetime.now(TIMEZONE)
    last_check = watch.get("last_checked_at")
    if last_check:
        try:
            last_dt = datetime.fromisoformat(last_check)
            diff = (now - last_dt).total_seconds()
        except Exception:
            diff = None
    else:
        diff = None
    if diff is not None and diff < (CHECK_INTERVAL_SECONDS * 0.33):
        continue
    watch["last_checked_at"] = now.isoformat()
    # Each user separately to respect per-user settings
    for user_id, watches in list(user_watches.items()):
        try:
            # user settings with defaults
            user_settings_for_id = user_settings.get(user_id, {})
            enabled_cats = user_settings_for_id.get("enabled_categories", CATEGORIES.copy())

            for watch in list(watches):
                try:
                    # skip non-active
                    if watch.get("status") != "active":
                        continue

                    # category check
                    watch_category = watch.get("category")
                    if watch_category and watch_category not in enabled_cats:
                        continue

                    # next_check backoff check
                    next_check = watch.get("next_check")
                    if next_check and now < next_check:
                        continue

                    # run scrape in executor so blocking IO doesn't block the loop
                    try:
                        new_data = await asyncio.get_event_loop().run_in_executor(None, scrape_product, watch["url"])
                    except Exception as exc:
                        await handle_watch_failure(context, user_id, watch, exc=exc, reason="scrape_error")
                        continue

                    # if no usable price -> consider delisted/OOS
                    if not new_data or new_data.get("current_price") is None:
                        if NOTIFY_ON_DELISTED:
                            await safe_send(
                                context.bot,
                                user_id,
                                f"⚠️ *{watch.get('title', 'Product')}* delisted or out of stock — pausing watch.\n{watch['url']}",
                                parse_mode="Markdown"
                            )
                        watch["status"] = "paused"
                        continue

                    # success: reset failure counters
                    watch["fail_count"] = 0
                    watch.pop("next_check", None)

                    old_price = watch.get("last_price")
                    current_price = new_data["current_price"]

                    # if no previous stored price for this watch, seed and skip alerts
                    if old_price is None:
                        watch["last_price"] = current_price
                        continue

                    # compute changes
                    changes = compute_changes(
                        {"current_price": old_price, "stock_status": watch.get("last_stock", "available")},
                        {"current_price": current_price, "stock_status": new_data.get("stock_status", "available")}
                    )

                    if not changes.get("significant_change"):
                        # update last seen and continue
                        watch["last_price"] = current_price
                        watch["last_stock"] = new_data.get("stock_status", "available")
                        continue

                    price_diff_percent = changes.get("price_diff_percent", 0.0)  # positive => drop
                    direction = watch.get("direction", "low")
                    deal_score = calculate_deal_score(price_diff_percent)

                    # Trigger logic based on direction or target_price
                    trigger = False
                    if direction == "low" and price_diff_percent > 0:
                        trigger = True
                    elif direction == "high" and price_diff_percent < 0:
                        trigger = True
                    elif direction == "both":
                        trigger = True

                    # Target price override
                    target_price = watch.get("target_price")
                    target_hit = False
                    try:
                        if target_price is not None and current_price <= target_price:
                            target_hit = True
                            trigger = True
                    except Exception:
                        pass

                    if not trigger:
                        watch["last_price"] = current_price
                        watch["last_stock"] = new_data.get("stock_status", "available")
                        continue

                    # Build enriched alert data and send
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

                    # update watch snapshot
                    watch["last_price"] = current_price
                    watch["last_stock"] = new_data.get("stock_status", "available")

                except Exception as exc_inner:
                    LOG.exception("Unexpected scheduler error user=%s watch=%s", user_id, watch.get("url"))
                    await handle_watch_failure(context, user_id, watch, exc=exc_inner, reason="unexpected")

        except Exception as exc_user:
            LOG.exception("Unexpected error processing watches for user=%s", user_id)
            # can't continue for this user — move to next


async def check_and_post_channel_deals(context: ContextTypes.DEFAULT_TYPE):
    """
    Unified channel posting:
      - Scrapes every ref in CHANNEL_MONITORED_URLS (via scrape_site/scrape_product)
      - Groups by normalize_product_key()
      - Posts one unified comparison per product group if drop/savings thresholds met.
    """
    global channel_posted_today, last_cache_date, channel_price_cache

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID or not CHANNEL_MONITORED_URLS:
        return

    # daily reset
    today = datetime.now(TIMEZONE).date()
    if last_cache_date != today:
        channel_posted_today.clear()
        last_cache_date = today

    loop = asyncio.get_event_loop()
    grouped = {}

    # 1) fetch (run blocking scrapes in executor)
    for ref in CHANNEL_MONITORED_URLS:
        try:
            # prefer scrape_site if available; fallback to scrape_product
            scrape_fn = scrape_site if "scrape_site" in globals() else scrape_product
            data = await loop.run_in_executor(None, scrape_fn, ref)
        except Exception as e:
            LOG.warning("Channel scrape failed for %s: %s", ref, e)
            continue

        # normalize numeric price
        try:
            cur = float(data.get("current_price")) if data.get("current_price") is not None else None
        except Exception:
            cur = None
        if cur is None:
            # skip delisted / no price
            continue

        data["current_price"] = cur
        data["url"] = data.get("url") or ref
        data["site"] = data.get("site") or (_get_domain_from_url(data["url"]) if "_get_domain_from_url" in globals() else data["url"])

        # build group key
        try:
            key = normalize_product_key(data)
        except Exception:
            key = f"UNK::{data['site']}::{int(cur)}"

        grouped.setdefault(key, []).append({"ref": ref, "data": data, "price": cur})

    # 2) evaluate groups and post at most MAX_CHANNEL_POSTS_PER_RUN
    posted_count = 0
    for product_key, entries in grouped.items():
        if posted_count >= (MAX_CHANNEL_POSTS_PER_RUN or 10):
            break
        try:
            # find best (lowest) price across entries and site-specific minima
            site_prices = {}
            for e in entries:
                site = e["data"]["site"]
                site_prices[site] = min(site_prices.get(site, float("inf")), e["price"])

            best_site, best_price = min(site_prices.items(), key=lambda kv: kv[1])

            # compute highest historical price among these refs (to measure drop)
            highest_old = 0
            for e in entries:
                old = channel_price_cache.get(e["ref"]) or 0
                highest_old = max(highest_old, old)

            if highest_old <= 0:
                # seed cache and skip first-time notifications
                for e in entries:
                    channel_price_cache[e["ref"]] = e["price"]
                continue

            drop_pct = round(((highest_old - best_price) / highest_old) * 100, 1)
            savings = highest_old - best_price

            if drop_pct < (MIN_DROP_PERCENT_FOR_CHANNEL or 5.0) or savings < (MIN_SAVINGS_FOR_CHANNEL or 15000):
                # update cache and skip
                for e in entries:
                    channel_price_cache[e["ref"]] = e["price"]
                continue

            # score with existing helper
            score = calculate_deal_score(drop_pct)
            if score not in ("high", "medium"):
                for e in entries:
                    channel_price_cache[e["ref"]] = e["price"]
                continue

            # dedupe: if any of the refs were posted today, skip (avoid duplication)
            if any(e["ref"] in channel_posted_today for e in entries):
                for e in entries:
                    channel_price_cache[e["ref"]] = e["price"]
                continue

            # Build message: unified comparison
            prod_title = entries[0]["data"].get("title") or product_key
            lines = []
            lines.append(f"🔥 *{prod_title}* — BEST PRICE: {_safe_currency(best_price)} on *{best_site}*")
            lines.append(f"📉 Drop: *{drop_pct}%* • Saved: *{_safe_currency(savings)}*")
            lines.append("")
            lines.append("💱 Prices across monitored sites:")
            for site, price in sorted(site_prices.items(), key=lambda kv: kv[1]):
                lines.append(f"- *{site}*: {_safe_currency(price)}")

            # prefer the first entry that matches best_site to provide a link
            best_entry = next((e for e in entries if e["data"]["site"] == best_site), entries[0])
            lines.append("")
            lines.append(f"🛒 Buy: {best_entry['data'].get('url')}")
            lines.append("")
            lines.append("⏰ Spotted now — prices move fast. Personal alerts → @YourBotUsername")

            msg = "\n".join(lines)

            await safe_send(context.bot, CHANNEL_DEAL_CHAT_ID, msg,
                            parse_mode="Markdown", disable_web_page_preview=True)

            # update caches and posted set
            for e in entries:
                channel_price_cache[e["ref"]] = e["price"]
                channel_posted_today.add(e["ref"])
            posted_count += 1

        except Exception:
            LOG.exception("Failed evaluating channel product %s", product_key)
            continue


async def check_trials(context: ContextTypes.DEFAULT_TYPE):
    """Validate trials and downgrade users whose trial expired."""
    bot = context.bot
    now = time.time()

    for user_id, sub in list(user_subscriptions.items()):
        try:
            if not sub:
                continue
            if "trial_start" in sub and not sub.get("paid", False):
                days_since = (now - sub["trial_start"]) / 86400
                tier = sub["tier"]
                trial_days = PAID_TIERS.get(tier, {"trial_days": 0})["trial_days"]

                if days_since > trial_days:
                    sub["tier"] = "free"
                    sub.pop("trial_start", None)

                    # Notify
                    msg = f"⚠️ Your {tier.capitalize()} trial expired. Downgraded to free tier."
                    try:
                        await safe_send(bot, user_id, msg)
                    except Exception:
                        LOG.exception("Failed to notify user about trial expiry")

                    # Enforce watch limit
                    watches = user_watches.get(user_id, [])
                    if len(watches) > MAX_WATCHES_FREE:
                        extra = len(watches) - MAX_WATCHES_FREE
                        msg2 = f"\nPlease remove {extra} watches to comply with the free tier limit."
                        try:
                            await safe_send(bot, user_id, msg2)
                        except Exception:
                            LOG.exception("Failed to notify user about watches limit after downgrade")
        except Exception:
            LOG.exception("Error while checking trials for user %s", user_id)
            continue


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.exception("Unhandled handler error: %s", context.error)


def run_bot():
    """Start the Telegram bot application and scheduler jobs."""
    global application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # register handlers
    for handler in get_application_handlers():
        application.add_handler(handler)

    application.add_error_handler(global_error_handler)

    # schedule jobs
    application.job_queue.run_repeating(
        callback=check_all_watches,
        interval=CHECK_INTERVAL_SECONDS,
        first=30,
        name="price_checker"
    )

    application.job_queue.run_repeating(
        callback=check_and_post_channel_deals,
        interval=CHECK_INTERVAL_SECONDS,
        first=90,
        name="channel_deals"
    )

    application.job_queue.run_repeating(
        callback=check_trials,
        interval=86400,  # daily
        first=3600,
        name="trial_checker"
    )

    LOG.info("Bot + scheduler started")
    application.run_polling(
    drop_pending_updates=True,
    allowed_updates=["message", "callback_query"]  # limit to what you handle
)