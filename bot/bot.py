import logging
import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
import nest_asyncio
from telegram.ext import Application, ContextTypes
import os
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
nest_asyncio.apply()

async def safe_send(bot, chat_id: int | list[int], text: str, **kwargs):
    """
    Enhanced Safe message sender.
    If chat_id is a list, it iterates through and sends to all.
    Logs full exception info and returns per-target send result.
    """
    results = []
    targets = chat_id if isinstance(chat_id, (list, tuple)) else [chat_id]

    for target in targets:
        try:
            await bot.send_message(chat_id=target, text=text, **kwargs)
            LOG.info("safe_send: message sent to %s", target)
            results.append((target, True, None))
        except Exception as exc:
            # Log details — Telegram exceptions often contain .message or .response
            try:
                LOG.exception("safe_send: failed sending to %s: %s", target, exc)
            except Exception:
                LOG.error("safe_send: failed to log exception for %s", target)
            results.append((target, False, str(exc)))
    return results


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

async def check_and_post_channel_deals(context, force: bool = False, send_delay_seconds: int | None = None):
    """
    Monitors monitored URLs, groups them by product, and posts significant deals.
    Added: async sleep between posted messages (non-blocking), configurable via param or env.
    - force: bypasses gating logic (as before)
    - send_delay_seconds: seconds to wait AFTER each posted message (default: env CHANNEL_SEND_DELAY_SECONDS or 60)
    """
    start_time = time.time()
    LOG.info("--- CHANNEL DEALS JOB STARTED ---")

    # 0. Resolve delay setting (param > env > default 60s)
    env_delay = os.getenv("CHANNEL_SEND_DELAY_SECONDS")
    if send_delay_seconds is None:
        try:
            send_delay_seconds = int(env_delay) if env_delay is not None and env_delay != "" else 60
        except Exception:
            send_delay_seconds = 60

    # Ensure non-negative integer
    try:
        send_delay_seconds = max(0, int(send_delay_seconds))
    except Exception:
        send_delay_seconds = 60

    LOG.info("Channel send delay set to %ds", send_delay_seconds)

    # 1. Initialization & Force Mode Check
    env_force = os.getenv("FORCE_CHANNEL_POSTS", "").lower() in ("1", "true", "yes")
    force_mode = force or env_force

    if force_mode:
        LOG.warning("channel_deals: FORCE MODE ENABLED — posting regardless of dedupe/threshold checks")

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.info("Channel posting disabled or missing config. Skipping.")
        return

    # 2. Scrape & Group (Consolidate multiple links for the same product)
    grouped: Dict[str, list] = {}
    for url in CHANNEL_MONITORED_URLS:
        try:
            # Use executor to avoid blocking if scrape_product is blocking
            loop = asyncio.get_event_loop()
            data = await loop.run_in_executor(None, scrape_product, url)
            if not data or data.get("current_price") is None:
                LOG.debug("No data/price for %s — skipping", url)
                continue

            key = normalize_product_key(data)
            grouped.setdefault(key, []).append({"url": url, "data": data})
        except Exception as e:
            LOG.warning("Scrape failed for %s: %s", url, e)

    # 3. Process Product Groups
    posted_count = 0
    now = datetime.now(timezone.utc)

    for product_key, entries in grouped.items():
        if posted_count >= (MAX_CHANNEL_POSTS_PER_RUN or 10):
            LOG.info("Reached max posts per run (%d) — stopping", posted_count)
            break

        # A. Determine Best Price in Group
        # Normalize prices defensively
        try:
            best_entry = min(entries, key=lambda x: float(x["data"].get("current_price") or float("inf")))
        except Exception:
            LOG.warning("Could not determine best entry for %s — skipping", product_key)
            continue

        best_price = float(best_entry["data"]["current_price"] or 0.0)
        best_site = best_entry["data"].get("site", "unknown")
        is_crypto = "binance" in (best_site or "").lower()

        # B. Load Snapshots & History
        old_prices = []
        last_posted_at = None

        for e in entries:
            snap = await load_channel_snapshot(e["url"])
            if snap:
                if snap.get("current_price"):
                    try:
                        old_prices.append(float(snap["current_price"]))
                    except Exception:
                        pass
                if snap.get("last_posted_at"):
                    try:
                        posted_dt = datetime.fromisoformat(snap["last_posted_at"])
                        if not last_posted_at or posted_dt > last_posted_at:
                            last_posted_at = posted_dt
                    except Exception:
                        pass

        highest_old = max(old_prices or [0.0])
        is_new = highest_old <= 0

        # Calculate changes relative to history
        drop_pct = round(((highest_old - best_price) / highest_old) * 100, 2) if not is_new and highest_old > 0 else 0.0
        savings = highest_old - best_price

        # C. Bulletproof Gating Logic (honoring force_mode)
        if not force_mode:
            cooldown_period = timedelta(hours=4 if is_crypto else 24)
            time_since_last_post = (now - last_posted_at) if last_posted_at else timedelta(days=99)
            in_cooldown = time_since_last_post < cooldown_period

            if is_crypto:
                CRYPTO_THRESHOLD = 5.0
                if abs(drop_pct) < CRYPTO_THRESHOLD and not is_new:
                    LOG.debug("Crypto %s change %s%% below threshold %s%% — skipping", product_key, drop_pct, CRYPTO_THRESHOLD)
                    continue
            elif in_cooldown:
                EXTRA_DROP_THRESHOLD = 5.0
                if drop_pct >= EXTRA_DROP_THRESHOLD:
                    LOG.info("OVERRIDE: %s hit extra drop %s%% inside cooldown — allowing post", product_key, drop_pct)
                else:
                    LOG.debug("In cooldown for %s and no extra drop — skipping", product_key)
                    continue
            else:
                if not is_new:
                    if drop_pct < (MIN_DROP_PERCENT_FOR_CHANNEL or 3.0):
                        LOG.debug("%s drop %s%% below MIN_DROP_PERCENT_FOR_CHANNEL — skipping", product_key, drop_pct)
                        continue
                    if savings < (MIN_SAVINGS_FOR_CHANNEL or 5000):
                        LOG.debug("%s savings %s below MIN_SAVINGS_FOR_CHANNEL — skipping", product_key, savings)
                        continue
                if drop_pct <= 0 and not is_new:
                    LOG.debug("%s price increased or no drop — skipping", product_key)
                    continue

        # D. Construct Professional Message
        title = (best_entry["data"].get("title") or "Product").strip()
        title_display = title.upper() if not is_crypto else title

        if is_crypto:
            direction = "🚀 PUMPING" if drop_pct < 0 else "📉 DUMPING"
            header = f"📊 CRYPTO ALERT: {direction}"
            price_line = f"💰 Price: {_safe_currency(best_price)}"
            stat_line = f"Change: {abs(drop_pct)}%"
        else:
            header = "🆕 NEW DEAL SPOTTED" if is_new else f"🔥 PRICE DROP: {drop_pct}% OFF"
            price_line = f"Now: {_safe_currency(best_price)}"
            stat_line = f"Saved: {_safe_currency(savings)}" if not is_new else "First time on radar"

        comparison = ""
        if not is_crypto and len(entries) > 1:
            comparison = "\n🏪 Price Comparison:\n"
            for e in sorted(entries, key=lambda x: float(x["data"].get("current_price") or float("inf"))):
                check = "✅" if float(e["data"].get("current_price") or 0) == best_price else "🔹"
                comparison += f"{check} {e['data'].get('site')}: {_safe_currency(e['data'].get('current_price'))}\n"

        msg = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"📦 {title_display}\n\n"
            f"{price_line}\n"
            f"{stat_line}\n"
            f"{comparison}"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔗 [View on {best_site}]({best_entry['url']})\n"
            f"📢 Personal Alerts: Soon...."
        )

        # E. Execution & Snapshot Update
        targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

        try:
            send_results = await safe_send(context.bot, targets, msg, parse_mode="Markdown", disable_web_page_preview=False)
        except Exception as e:
            LOG.exception("Failed to send message for %s: %s", product_key, e)
            send_results = [(t, False, str(e)) for t in targets]

        # If successfully sent to at least one target, update snapshots
        if any(res[1] for res in send_results):
            now_iso = now.isoformat()
            for e in entries:
                await save_channel_snapshot(e["url"], {**e["data"], "last_posted_at": now_iso})
            posted_count += 1

            # If delay is configured, sleep AFTER a successful post
            if send_delay_seconds and send_delay_seconds > 0:
                LOG.info("Posted product_key=%s — sleeping %ds before next post (to avoid rate limits)", product_key, send_delay_seconds)
                await asyncio.sleep(send_delay_seconds)

    LOG.info(f"--- JOB FINISHED: {posted_count} deals posted in {time.time()-start_time:.1f}s ---")

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


async def run_bot():
    """
    PTB v20+/v21-compatible run_bot intended to be launched as a background task
    (e.g. asyncio.create_task(run_bot()) from FastAPI startup).
    """
    global application

    # sanity
    if not TELEGRAM_TOKEN:
        LOG.error("TELEGRAM_TOKEN is empty — bot will not start.")
        return

    LOG.info("Building Telegram Application...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers and error handler
    for handler in get_application_handlers():
        application.add_handler(handler)
    application.add_error_handler(global_error_handler)

    # Register jobs BEFORE starting so job_queue is present when app starts
    if application.job_queue:
        application.job_queue.run_repeating(
            callback=check_all_watches,
            interval=CHECK_INTERVAL_SECONDS,
            first=30,
            name="price_checker"
        )
        application.job_queue.run_repeating(
            callback=check_and_post_channel_deals,
            interval=CHECK_INTERVAL_SECONDS,
            first=10,
            name="channel_deals"
        )
        application.job_queue.run_repeating(
            callback=check_trials,
            interval=86400,
            first=3600,
            name="trial_checker"
        )

    # Optional quick test job to confirm job queue is running (uncomment while debugging)
    # async def _test_job(ctx):
    #     LOG.info("TEST JOB tick: %s", datetime.now(timezone.utc).isoformat())
    # application.job_queue.run_repeating(_test_job, interval=10, first=5, name="test_job")

    # Attempt to remove webhook (polling is ignored if webhook is set)
    try:
        # application.bot may not be ready until initialize() but in practice builder sets it
        if application.bot:
            try:
                await application.bot.delete_webhook()
                LOG.info("Deleted existing Telegram webhook (if any).")
            except Exception as e:
                LOG.debug("delete_webhook() returned: %s", e)
    except Exception:
        LOG.debug("Could not check/delete webhook (not fatal).", exc_info=True)

    # Run polling (this will run until stopped); because run_bot() is started as a task,
    # awaiting run_polling() keeps the background task alive.
    try:
        LOG.info("Starting Application.run_polling() (PTB v20+/v21)...")
        # run_polling handles init/start/stop lifecycle internally
        await application.run_polling(drop_pending_updates=True)
        LOG.info("Application.run_polling() finished (bot stopped).")
    except asyncio.CancelledError:
        LOG.info("run_bot task cancelled — stopping application.")
        # ensure graceful stop
        try:
            await application.stop()
        except Exception:
            LOG.exception("Error while stopping application after cancellation.")
    except Exception as exc:
        LOG.exception("Unexpected exception in run_polling(): %s", exc)
        # Try graceful shutdown
        try:
            await application.stop()
        except Exception:
            LOG.exception("application.stop() failed after run_polling error.")
    finally:
        # Ensure resources cleaned
        try:
            await application.shutdown()
        except Exception:
            LOG.debug("application.shutdown() error (ignored).", exc_info=True)

    LOG.info("run_bot() exit complete.")  # Run forever