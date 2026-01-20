import logging
import asyncio
import time
from telegram import Update
from datetime import datetime, timedelta, timezone
from typing import Dict, Any
import nest_asyncio
from telegram.ext import Application, ContextTypes
import os
import redis
import sys
import redis.asyncio as redis_async
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
    _get_domain_from_url,
    scrape_fuel_prices,
)
from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    _db_ensure_table,
    delete_expired_channel_snapshots,
    wipe_channel_snapshots_redis,
)
from Utils.format import format_telegram_alert, _safe_currency, update_exchange_rate

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

application: Application | None = None  # global if needed elsewhere
nest_asyncio.apply()
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in ("1", "true", "yes")

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

async def check_and_post_channel_deals(context: ContextTypes.DEFAULT_TYPE):
    """
    Job: Scrapes monitored URLs, groups them, finds the best deal, 
    and posts to Telegram channels if a drop is detected or if it's a new item.
    """
    start_time = time.time()
    LOG.info("--- CHANNEL DEALS JOB STARTED ---")
    
    # Ensure exchange rates are current for currency formatting
    await update_exchange_rate()

    # 1. Config Guards
    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.info("Channel posting disabled or CHANNEL_DEAL_CHAT_ID missing — skipping")
        return

    max_posts = MAX_CHANNEL_POSTS_PER_RUN or 5
    send_delay = 15
    eligible_candidates = []
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    min_dt = datetime.min.replace(tzinfo=timezone.utc)

    # --- PHASE 1: SCRAPE EVERYTHING ---
    for group_key, urls in CHANNEL_MONITORED_URLS.items():
        entries = []
        for url in urls:
            try:
                # Scrape in executor to prevent blocking the event loop
                data = await asyncio.get_event_loop().run_in_executor(None, scrape_product, url)
                if data and data.get("current_price") is not None:
                    entries.append({"url": url, "data": data})
            except Exception as e:
                LOG.warning("Scrape failed for %s in group %s: %s", url, group_key, e)

        if not entries:
            LOG.info("Group '%s': No successful scrapes — skipping", group_key)
            continue

        # --- PHASE 2: DETERMINE GROUP HISTORY & BEST PRICE ---
        try:
            best_entry = min(entries, key=lambda e: float(e["data"]["current_price"]))
        except Exception:
            LOG.warning("Could not determine best price for group %s", group_key)
            continue

        current_price = float(best_entry["data"]["current_price"])

        # Check existing posting history for this group
        history_tuples = []
        valid_timestamps = []
        for entry in entries:
            snap = await load_channel_snapshot(entry["url"])
            at_str = snap.get("last_posted_at") if snap else None
            price_val = snap.get("last_posted_price") if snap else None
            history_tuples.append((at_str, price_val))

            if at_str:
                try:
                    dt = datetime.fromisoformat(at_str.replace("Z", "+00:00") if "Z" in at_str else at_str)
                    valid_timestamps.append(dt)
                except: pass

        is_crypto = any("binance" in (e["data"].get("site", "").lower()) or "SYMBOL:" in e["url"] for e in entries)
        non_empty_histories = [(at, p) for at, p in history_tuples if at is not None or p is not None]
        unique_histories = set(non_empty_histories)

        # Determine if this is a "New" deal or an update
        if not non_empty_histories:
            is_new = True
            last_posted_at_dt = min_dt
            last_posted_price = None
        elif len(unique_histories) <= 1:
            at_str, price_val = next(iter(unique_histories))
            is_new = (price_val is None)
            last_posted_price = float(price_val) if price_val is not None else None
            last_posted_at_dt = max(valid_timestamps) if valid_timestamps else min_dt
        else:
            # Inconsistent history (partial group updates) — force a sync post
            is_new = True
            last_posted_at_dt = min_dt
            last_posted_price = None

        # --- PHASE 3: ELIGIBILITY CHECK ---
        ref_price = last_posted_price if last_posted_price is not None else current_price
        price_change = round(((current_price - ref_price) / ref_price) * 100, 1) if ref_price != 0 else 0.0
        abs_change = abs(price_change)
        time_since_last = float('inf') if last_posted_at_dt == min_dt else (now - last_posted_at_dt).total_seconds()
        drop_pct = round(((ref_price - current_price) / ref_price) * 100, 1) if ref_price > current_price else 0.0
        savings = max(ref_price - current_price, 0)

        # Always post the very first time we see/track this group
        should_post = is_new

        if not is_new:
            # Only post on further drops (current best price < last posted price)
            if abs_change > 0:
                if is_crypto:
                    # Lighter threshold for crypto (volatile)
                    if abs_change >= 1.0 and time_since_last >= 21600:
                        should_post = True
                else:
                    # Use configured thresholds for regular deals
                    if price_change < 0 and -price_change >= MIN_DROP_PERCENT_FOR_CHANNEL and savings >= MIN_SAVINGS_FOR_CHANNEL:
                        should_post = True

        if should_post:
            eligible_candidates.append({
                "group_key": group_key,
                "best_entry": best_entry,
                "entries": entries,
                "last_posted_at": last_posted_at_dt,
                "stats": {"drop_pct": drop_pct, "change_pct": price_change, "is_new": is_new, "is_crypto": is_crypto, "savings": savings}
            })

    # --- PHASE 4: PRIORITIZE & POST ---
    # Prioritize biggest drops/savings first, then oldest posted time
    eligible_candidates.sort(key=lambda x: (
        -x["stats"]["drop_pct"],   # Biggest % drop first
        -x["stats"]["savings"],    # Biggest absolute savings
        x["last_posted_at"]        # Oldest last (for catch-up)
    ))
    to_post = eligible_candidates[:max_posts]
    
    if not to_post:
        LOG.info("No eligible deals found to post.")
        return

    posted_count = 0
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

    for item in to_post:
        best_entry = item["best_entry"]
        stats = item["stats"]
        group_key = item["group_key"]

        price = float(best_entry["data"]["current_price"])
        site = best_entry["data"].get("site", "unknown").upper()
        image = best_entry["data"].get("image")
        description = best_entry["data"].get("description", "").strip()
        title = (best_entry["data"].get("title") or group_key.replace("-", " ").title()).strip()

        # Build Comparison Lines
        comparison_lines = []
        try:
            sorted_entries = sorted(item["entries"], key=lambda e: float(e["data"].get("current_price") or float("inf")))
            best_price_val = float(sorted_entries[0]["data"]["current_price"])
        except:
            sorted_entries = item["entries"]
            best_price_val = price

        for e in sorted_entries:
            try:
                p = float(e["data"].get("current_price") or 0.0)
            except: p = 0.0
            site_label = e["data"].get("site", _get_domain_from_url(e["url"])).upper()
            rel_pct = round(((p - best_price_val) / best_price_val) * 100, 1) if best_price_val > 0 else 0.0
            mark = "✅ BEST" if p == best_price_val else ("⚠️ Good" if rel_pct <= 5.0 else "•")
            price_str = _safe_currency(p, site=site_label) if p else "N/A"
            comparison_lines.append(f"{mark} <a href=\"{e['url']}\">{site_label}</a>: {price_str}")

        comparison_text = "🏪 <b>Comparison:</b>\n" + "\n".join(comparison_lines) if comparison_lines else ""

        # Construct Caption HTML
        if stats["is_crypto"]:
            header = "🆕 NEW CRYPTO TRACKED" if stats["is_new"] else f"{'📈' if stats['change_pct'] > 0 else '📉'} NAIRA STRENGTH"
            caption = (
                f"<b>{header}</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"💰 Current: {_safe_currency(price, site=best_entry['data'].get('site', 'unknown'))}\n"
                f"📊 Change: {stats['change_pct']:+.1f}%\n\n"
                #f"{comparison_text}\n━━━━━━━━━━━━━━━━━━\n"
                f"🔗 <a href=\"{best_entry['url']}\">{site}</a>"
            )
        else:
            header = "🆕 NEW DEAL!" if stats["is_new"] else f"🔥 {stats['drop_pct']}% DROP!"
            caption = (
                f"<b>{header}</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"📦 {title}\n"
                f"💰 Now: {_safe_currency(price)}\n"
                f"📉 Saved: {_safe_currency(stats['savings'])}\n\n"
                f"{comparison_text}\n━━━━━━━━━━━━━━━━━━\n"
                f"🛒 <a href=\"{best_entry['url']}\">Shop on {site}</a>"
            )

        # Truncate Description to fit Telegram 1024 char limit for photos
        if description:
            max_desc_len = 1024 - len(caption) - 50
            if max_desc_len > 20:
                truncated = description[:max_desc_len].rstrip() + "..." if len(description) > max_desc_len else description
                caption += f"\n\n📄 <b>Product details:</b>\n<blockquote>{truncated}</blockquote>"

        caption += "\n\n🔔 @Real_Time_Alert" # Optional: add your handle

        # Send to targets
        sent_successfully = False
        for chat_id in targets:
            try:
                if image:
                    await context.bot.send_photo(chat_id=chat_id, photo=image, caption=caption, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=True)
                sent_successfully = True
                LOG.info("Posted %s to %s", group_key, chat_id)
            except Exception as e:
                LOG.error("Failed post %s to %s: %s", group_key, chat_id, e)

        # Update Database ONLY if at least one post was successful
        if sent_successfully:
            for e in item["entries"]:
                await save_channel_snapshot(
                    e["url"],
                    {"last_posted_at": now_iso, "last_posted_price": price},
                    expires_hours=168
                )
            posted_count += 1
            if posted_count < len(to_post):
                await asyncio.sleep(send_delay)

    LOG.info("--- JOB FINISHED: %d deals posted ---", posted_count)

async def check_and_post_fuel_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Daily fuel update job - uses persistent snapshots to ensure once-per-day posting.
    Prefers scrape_fuel_prices_many() (multi-source) but falls back to scrape_fuel_prices().
    """
    now = datetime.now(TIMEZONE)
    FUEL_TRACKING_KEY = "https://fuelpricewatch.com/nigeria"

    # Wake-up window: only run between 07:00-07:59 local TIMEZONE
    if not TEST_MODE:
        if not (7 <= now.hour < 8):
            LOG.debug("Outside of 7 AM window — skipping fuel update")
            return

    # Load last snapshot and short-circuit if already posted recently
    try:
        snapshot = await load_channel_snapshot(FUEL_TRACKING_KEY)
    except Exception:
        LOG.exception("Failed to load fuel snapshot")
        snapshot = None

    if snapshot and snapshot.get("last_posted_at"):
        if not TEST_MODE:
            try:
                last_run = datetime.fromisoformat(snapshot["last_posted_at"])
                if (now - last_run).total_seconds() < 72_000:
                    LOG.info("Fuel update already sent today at %s — skipping", last_run.isoformat())
                    return
            except Exception:
                LOG.warning("Could not parse last_posted_at; proceeding to scrape")

    # Scrape data (prefer many-source scraper)
    data = None
    try:
        # prefer the multi-source async entrypoint if available
        if "scrape_fuel_prices" in globals():
            data = await scrape_fuel_prices()
        else:
            # fallback to older scraper name (your codebase imported scrape_fuel_prices earlier)
            data = await scrape_fuel_prices_()
    except Exception:
        LOG.exception("Error during fuel price scraping")
        return

    # Normalize data to expected fields for formatting
    # two possible shapes:
    #  - {"avg_formatted": "₦123,456", "sources": [...], "avg_raw": 123456.0, "timestamp": "..."}
    #  - {"avg_petrol": "₦123,456", "change_today": "...", "last_updated": "..."}
    avg_formatted = None
    change_today = None
    last_updated = None
    sources = []

    if not data:
        LOG.warning("No data returned from scraper — skipping post")
        return

    # Multi-source shape
    if isinstance(data, dict) and "avg_formatted" in data:
        avg_formatted = data.get("avg_formatted") or "N/A"
        # some parsers return string prices inside sources; normalize them
        sources = data.get("sources", []) or []
        # try to derive change and last_updated from per-source last_updated if present
        last_updated = None
        for s in sources:
            if s.get("last_updated"):
                last_updated = s.get("last_updated")
                break
    else:
        # Legacy shape
        avg_formatted = data.get("avg_petrol") if isinstance(data, dict) else None
        change_today = data.get("change_today") if isinstance(data, dict) else None
        last_updated = data.get("last_updated") if isinstance(data, dict) else None
        # build a minimal sources array if possible
        if isinstance(data, dict):
            sources = []
            # prefer any explicit source items
            for key in ("source", "sources", "details"):
                val = data.get(key)
                if isinstance(val, list):
                    sources = val
                    break

    # final sanity
    if not avg_formatted or avg_formatted == "N/A":
        LOG.warning("Scraper returned no average petrol price — skipping post")
        return

    # compute confidence: count how many sources returned a usable price
    reported = 0
    total = max(1, len(sources))
    sources_lines = []
    for s in sources:
        # each s expected to have: source, price_raw/price_str, error, url
        src_name = s.get("source") or _get_domain_from_url(s.get("url") or "") or "source"
        price_str = s.get("price_str") or s.get("price_raw") or s.get("price") or None
        err = s.get("error")
        url = s.get("url") or ""
        if price_str and not err:
            reported += 1
            # normalize display string
            if isinstance(price_str, (int, float)):
                price_str = f"₦{int(price_str):,}"
            sources_lines.append(f"• {src_name} — {price_str} — <a href=\"{_safe_url(url)}\">link</a>")
        else:
            # show error label
            err_label = err or "no data"
            sources_lines.append(f"• {src_name} — {err_label}")

    confidence = f"{reported}/{total}" if total else f"{reported}/4"

    # use provided change_today if available, else try to infer small delta from sources (best-effort)
    change_text = change_today or data.get("change_today") or "No change"
    last_updated_text = last_updated or data.get("timestamp") or now.strftime("%b %d, %H:%M")

    # Build message (HTML) in your requested format
    message_lines = [
        "🌅 <b>Fuel Price Report — Nigeria</b>",
        f"📅 {now.strftime('%b %d, %Y')} — <i>Morning update</i>",
        "━━━━━━━━━━━━━━━━━━",
        f"⛽ <b>National Avg (PMS):</b> <b>{avg_formatted}</b>",
    ]

    # If change_text is a numeric or a small string, present it; allow both forms like "-₦1,200 (-0.7%)" or "No change"
    message_lines.append(f"📉 <b>Change today:</b> {change_text}")
    message_lines.append(f"🕒 <b>Last updated:</b> {last_updated_text}")
    message_lines.append(f"🔎 <b>Confidence:</b> {confidence} sources reported")
    message_lines.append("")  # blank
    message_lines.append("🏷️ <b>Sources</b>")

    # Append each source line
    message_lines.extend(sources_lines or ["• FuelPriceWatch — data unavailable"])

    message_lines.append("")  # blank
    message_lines.append("━━━━━━━━━━━━━━━━━━")
    message_lines.append("<i>Tip:</i> tap a source to view the bulletin. 🔗")

    message = "\n".join(message_lines)

    # Send to channel(s)
    try:
        sent_results = await safe_send(
            context.bot,
            CHANNEL_DEAL_CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
    except Exception:
        LOG.exception("Failed to safe_send fuel update")
        sent_results = []

    # Persist snapshot only if at least one send succeeded
    if any(res[1] for res in sent_results if isinstance(res, (list, tuple))):
        try:
            await save_channel_snapshot(
                FUEL_TRACKING_KEY,
                {
                    "last_posted_at": now.isoformat(),
                    "last_posted_price": avg_formatted,
                    "sources_confidence": confidence,
                },
                expires_hours=48
            )
            LOG.info("Fuel update posted and snapshot saved.")
        except Exception:
            LOG.exception("Failed to save fuel snapshot after posting")
    else:
        LOG.warning("No successful sends recorded; snapshot not updated.")




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


async def acquire_long_running_lock(r: redis_async.Redis, lock_name: str = "telegram-bot-single-instance"):
    """
    Acquires & auto-renews an **asynchronous** Redis lock.
    Uses redis.asyncio's native async lock implementation.
    """
    # Create async lock
    lock = r.lock(lock_name, timeout=10)  # timeout = TTL in seconds

    # Try to acquire immediately (non-blocking)
    max_retries = 5
    for i in range(max_retries):
        acquired = await lock.acquire(blocking=False)
        if acquired:
            break
        LOG.warning(f"Lock taken, retry {i+1}/{max_retries} in 5s...")
        await asyncio.sleep(5)
    else:
        # If we exhausted retries
        LOG.error("Could not acquire lock after retries — exiting")
        return False, None

    LOG.info("Async lock acquired — starting renewal task")

    async def renew_loop():
        try:
            while True:
                await asyncio.sleep(4)  # renew more frequently than TTL/2
                if await lock.owned():
                    await lock.extend(10)  # extend TTL
                    LOG.debug("Async lock renewed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.warning("Async lock renewal failed: %s", e)

    renewal_task = asyncio.create_task(renew_loop())

    return True, (lock, renewal_task)


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.exception("Unhandled handler error: %s", context.error)


async def run_bot():
    """
    PTB v20+/v21-compatible run_bot intended to be launched as a background task.
    """
    global application

    if not TELEGRAM_TOKEN:
        LOG.error("TELEGRAM_TOKEN is empty — bot will not start.")
        return

    REDIS_URL = os.getenv("REDIS_URL")
    if not REDIS_URL:
        LOG.error("REDIS_URL not set")
        return

    r = None
    lock = None
    renewal_task = None

    try:
        r = await redis_async.from_url(REDIS_URL, decode_responses=True)

        lock_acquired, lock_info = await acquire_long_running_lock(r)
        if not lock_acquired:
            LOG.warning("Could not acquire lock → exiting")
            return

        lock, renewal_task = lock_info

        # ── Bot initialization ────────────────────────────────────────
        LOG.info("Building Telegram Application...")
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        # Explicit initialize (recommended)
        await application.initialize()

        # Add handlers & error handler
        for handler in get_application_handlers():
            application.add_handler(handler)
        application.add_error_handler(global_error_handler)
        
        if os.getenv("WIPE_CHANNEL_REDIS") == "1":
            wipe_channel_snapshots_redis(dry_run=False)
        
        # Register jobs
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
                callback=check_and_post_fuel_prices,
                interval=120,
                #time=dt_time(hour=7, minute=0, second=0, tzinfo=TIMEZONE),
                name="check_fuel_prices",
                first=10,
            )
            application.job_queue.run_repeating(
                callback=check_trials,
                interval=86400,
                first=3600,
                name="trial_checker"
            )

        # Clean webhook if exists
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
            LOG.info("Webhook cleared (if any existed)")
        except Exception as e:
            LOG.debug("Webhook cleanup: %s", e)

        # Start the bot and polling
        LOG.info("Starting long-running polling...")
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

        # Keep running until cancelled
        await asyncio.Event().wait()  # Wait forever

    except asyncio.CancelledError:
        LOG.info("run_bot task cancelled — graceful shutdown")

    except Exception as exc:
        LOG.exception("Fatal error in run_bot: %s", exc)

    finally:
        LOG.info("Cleaning up resources...")

        # Stop application
        if application:
            try:
                if application.updater:
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception as e:
                LOG.warning("Application shutdown error: %s", e)

        # Cancel renewal task
        if renewal_task:
            renewal_task.cancel()

        # Release Redis lock
        if lock:
            try:
                if await lock.owned():
                    await lock.release()
                    LOG.info("Redis lock released")
            except Exception as e:
                LOG.warning("Failed to release lock: %s", e)

        # Close Redis connection
        if r:
            await r.aclose()
            LOG.info("Redis connection closed")

    LOG.info("run_bot finished.")  # Run forever