import logging
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

from telegram.ext import Application, ContextTypes

from Utils.config import TELEGRAM_TOKEN, DB_URL
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

from Utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    _get_domain_from_url,  # helper from utils (if present)
)
from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    _db_ensure_table,
    delete_expired_channel_snapshots,
)
from Utils.format import format_telegram_alert, _safe_currency

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

                    now = datetime.now(TIMEZONE)

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

                    # --- LOAD previous snapshot (redis 1st, db fallback) ---
                    try:
                        prev_snapshot = await load_last_snapshot(watch["url"])
                    except Exception:
                        LOG.exception("Failed loading last snapshot for %s", watch.get("url"))
                        prev_snapshot = None

                    old_price = None
                    old_stock = watch.get("last_stock", "available")
                    last_checked_iso = None

                    if prev_snapshot:
                        old_price = prev_snapshot.get("current_price")
                        old_stock = prev_snapshot.get("stock_status", old_stock)
                        last_checked_iso = prev_snapshot.get("last_checked_at")

                    # mark last_checked logic (avoid duplicate immediate runs)
                    if last_checked_iso:
                        try:
                            last_dt = datetime.fromisoformat(last_checked_iso)
                            if (now - last_dt).total_seconds() < (CHECK_INTERVAL_SECONDS * 0.33):
                                # skip if we checked recently (self-heal)
                                continue
                        except Exception:
                            # if parsing fails, ignore and proceed
                            pass

                    current_price = new_data["current_price"]

                    # If no previous stored price, seed snapshot then continue (no alert first time)
                    if old_price is None:
                        try:
                            await save_snapshot({
                                "url": watch["url"],
                                "site": new_data.get("site"),
                                "title": new_data.get("title"),
                                "current_price": current_price,
                                "previous_price": new_data.get("previous_price"),
                                "stock_status": new_data.get("stock_status"),
                                "raw": new_data.get("raw"),
                            })
                        except Exception:
                            LOG.exception("Failed saving initial snapshot for %s", watch.get("url"))

                        # update in-memory watch for immediate scheduling
                        watch["last_price"] = current_price
                        watch["last_stock"] = new_data.get("stock_status")
                        watch["last_checked_at"] = datetime.now(TIMEZONE).isoformat()
                        continue

                    # compute changes
                    changes = compute_changes(
                        {"current_price": old_price, "stock_status": old_stock},
                        {"current_price": current_price, "stock_status": new_data.get("stock_status", "available")}
                    )

                    if not changes.get("significant_change"):
                        # update last seen and persist snapshot (update last_checked_at)
                        watch["last_price"] = current_price
                        watch["last_stock"] = new_data.get("stock_status", "available")
                        watch["last_checked_at"] = datetime.now(TIMEZONE).isoformat()
                        try:
                            await save_snapshot({
                                "url": watch["url"],
                                "site": new_data.get("site"),
                                "title": new_data.get("title"),
                                "current_price": current_price,
                                "previous_price": old_price,
                                "stock_status": new_data.get("stock_status"),
                                "raw": new_data.get("raw"),
                            })
                        except Exception:
                            LOG.exception("Failed saving snapshot (no-significant-change) for %s", watch.get("url"))
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
                        # persist snapshot update
                        try:
                            await save_snapshot({
                                "url": watch["url"],
                                "site": new_data.get("site"),
                                "title": new_data.get("title"),
                                "current_price": current_price,
                                "previous_price": old_price,
                                "stock_status": new_data.get("stock_status"),
                                "raw": new_data.get("raw"),
                            })
                        except Exception:
                            LOG.exception("Failed saving snapshot (post-no-trigger) for %s", watch.get("url"))
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

                    # update watch snapshot (persist current state)
                    watch["last_price"] = current_price
                    watch["last_stock"] = new_data.get("stock_status", "available")
                    watch["last_checked_at"] = datetime.now(TIMEZONE).isoformat()
                    try:
                        await save_snapshot({
                            "url": watch["url"],
                            "site": new_data.get("site"),
                            "title": new_data.get("title"),
                            "current_price": current_price,
                            "previous_price": old_price,
                            "stock_status": new_data.get("stock_status"),
                            "raw": new_data.get("raw"),
                        })
                    except Exception:
                        LOG.exception("Failed saving snapshot after alert for %s", watch.get("url"))

                except Exception as exc_inner:
                    LOG.exception("Unexpected scheduler error user=%s watch=%s", user_id, watch.get("url"))
                    await handle_watch_failure(context, user_id, watch, exc=exc_inner, reason="unexpected")

        except Exception as exc_user:
            LOG.exception("Unexpected error processing watches for user=%s", user_id)
            # can't continue for this user — move to next


async def check_and_post_channel_deals(context: ContextTypes.DEFAULT_TYPE):
    """
    Unified channel posting – now uses persistent channel snapshots (Redis + DB fallback).
    - Price history persists across restarts.
    - Daily deduplication persists across restarts (via last_posted_at in Redis payload).
    """
    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID or not CHANNEL_MONITORED_URLS:
        return

    loop = asyncio.get_event_loop()
    grouped: Dict[str, list] = {}

    # 1) Scrape all references
    for ref in CHANNEL_MONITORED_URLS:
        try:
            data = await loop.run_in_executor(None, scrape_product, ref)
        except Exception as e:
            LOG.warning("Channel scrape failed for %s: %s", ref, e)
            continue

        try:
            cur = float(data.get("current_price")) if data.get("current_price") is not None else None
        except Exception:
            cur = None

        if cur is None:
            continue

        data["current_price"] = cur
        data["url"] = data.get("url") or ref
        data["site"] = data.get("site") or (_get_domain_from_url(data["url"]) if "_get_domain_from_url" in globals() else "Unknown")

        try:
            key = normalize_product_key(data)
        except Exception as exc:
            LOG.warning("Normalize key failed for %s: %s", ref, exc)
            key = f"UNK::{data['site']}::{int(cur)}"

        grouped.setdefault(key, []).append({"ref": ref, "data": data, "price": cur})

    # 2) Process groups
    posted_count = 0
    today_str = datetime.now(timezone.utc).date().isoformat()  # 'YYYY-MM-DD'

    for product_key, entries in grouped.items():
        if posted_count >= (MAX_CHANNEL_POSTS_PER_RUN or 10):
            break

        # Site → lowest price on that site
        site_prices: Dict[str, float] = {}
        for e in entries:
            site = e["data"]["site"]
            site_prices[site] = min(site_prices.get(site, float("inf")), e["price"])

        best_price = min(site_prices.values())
        best_site = min(site_prices, key=site_prices.get)

        # Load previous snapshots to get old prices + check if posted today
        old_prices = []
        any_posted_today = False

        for e in entries:
            old_snap = await load_channel_snapshot(e["ref"])
            if old_snap:
                old_p = old_snap.get("current_price")
                if old_p is not None:
                    old_prices.append(old_p)

                lpa = old_snap.get("last_posted_at")
                if lpa and lpa.startswith(today_str):  # date match
                    any_posted_today = True

        highest_old = max(old_prices or [0.0])

        # First-time seen → seed and skip alert
        if highest_old <= 0:
            for e in entries:
                await save_channel_snapshot(e["ref"], e["data"])
            continue

        drop_pct = round(((highest_old - best_price) / highest_old) * 100, 1)
        savings = highest_old - best_price

        # No price drop → just update snapshots
        if drop_pct <= 0:
            for e in entries:
                await save_channel_snapshot(e["ref"], e["data"])
            continue

        # Threshold / score checks
        if drop_pct < (MIN_DROP_PERCENT_FOR_CHANNEL or 5.0) or savings < (MIN_SAVINGS_FOR_CHANNEL or 15000):
            for e in entries:
                await save_channel_snapshot(e["ref"], e["data"])
            continue

        score = calculate_deal_score(drop_pct)
        if score not in ("high", "medium"):
            for e in entries:
                await save_channel_snapshot(e["ref"], e["data"])
            continue

        # Already posted today → update but don't post again
        if any_posted_today:
            for e in entries:
                await save_channel_snapshot(e["ref"], e["data"])
            continue

        # === POST DEAL ===
        prod_title = entries[0]["data"].get("title") or product_key
        lines = [
            f"🔥 *{prod_title}* — BEST PRICE: {_safe_currency(best_price)} on *{best_site}*",
            f"📉 Drop: *{drop_pct}%* • Saved: *{_safe_currency(savings)}*",
            "",
            "💱 Prices across monitored sites:",
        ]
        for site, price in sorted(site_prices.items(), key=lambda kv: kv[1]):
            lines.append(f"- *{site}*: {_safe_currency(price)}")

        best_entry = next((e for e in entries if e["data"]["site"] == best_site), entries[0])
        lines += [
            "",
            f"🛒 Buy: {best_entry['data'].get('url')}",
            "",
            "⏰ Spotted now — prices move fast. Personal alerts → @YourBotUsername"
        ]

        msg = "\n".join(lines)

        await safe_send(
            context.bot,
            CHANNEL_DEAL_CHAT_ID,
            msg,
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

        # Save with last_posted_at (persistent dedupe)
        now_iso = datetime.now(timezone.utc).isoformat()
        for e in entries:
            posted_data = {**e["data"], "last_posted_at": now_iso}
            await save_channel_snapshot(e["ref"], posted_data)

        posted_count += 1


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
        name="channel_deals",
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