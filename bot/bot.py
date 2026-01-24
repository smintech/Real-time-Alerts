import logging
import asyncio
from telegram import Update
from datetime import datetime, timedelta, timezone, time
import time as std_time
from typing import Dict, Optional, Any, Tuple, Callable, List
import nest_asyncio
from telegram.ext import Application, ContextTypes
import os
from bs4 import BeautifulSoup
import redis
import sys
import hashlib
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
    DEFAULT_SCHOOL_SOURCES,
)

from Utils.utils import (
    scrape_product,
    compute_changes,
    calculate_deal_score,
    normalize_product_key,
    _get_domain_from_url,
    scrape_fuel_prices,
    _format_naira,
    scrape_lpg_prices,
    _fetch_html,
    _extract_snippets_from_html,
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
SCHOOL_FORCE_POST = os.getenv("SCHOOL_UPDATES_FORCE", "false").lower() in ("1", "true", "yes") 
SCHOOL_UPDATES_KEY = "nigeria_school_updates_report"

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

def _hash_report_content(report_text: str) -> str:
    """Simple SHA256 hash of the report content for change detection."""
    return hashlib.sha256(report_text.encode("utf-8")).hexdigest()

def _slugify(text: str) -> str:
    """Simple slugify for snapshot keys."""
    return re.sub(r'[^a-z0-9_]', '', text.lower().replace(' ', '_').replace('-', '_'))

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
    start_time = std_time.time()
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

def _safe_url(u: str) -> str:
    """
    Simple safety wrapper for URLs in HTML links.
    Prevents broken/malformed links and avoids empty hrefs.
    """
    if not u:
        return ""
    u = u.strip()
    if u.startswith(("http://", "https://")):
        return u
    # If no scheme, add https (most sites work)
    return f"https://{u}"

def log_fuel_scraper_data(data: Any, *, test_mode: bool = False) -> None:
    """
    Log a compact, safe summary of fuel scraper output.
    Standalone: no return value, no dependency on other helpers.
    """

    try:
        if not isinstance(data, dict):
            LOG.warning(
                "Fuel scraper returned non-dict data: type=%s value=%r",
                type(data).__name__,
                str(data)[:200],
            )
            return

        summary = {
            "keys": list(data.keys()),
            "avg_petrol": data.get("avg_petrol"),
            "avg_formatted": data.get("avg_formatted"),
            "avg_raw": data.get("avg_raw"),
            "change_today": data.get("change_today"),
            "last_updated": data.get("last_updated"),
            "timestamp": data.get("timestamp"),
            "sources_count": len(data.get("sources") or []),
        }

        sources_preview = []
        for s in (data.get("sources") or [])[:5]:
            if not isinstance(s, dict):
                sources_preview.append({"type": type(s).__name__})
                continue

            sources_preview.append({
                "source": s.get("source"),
                "url": s.get("url"),
                "price_raw": s.get("price_raw"),
                "price_str": s.get("price_str"),
                "error": s.get("error"),
            })

        summary["sources_preview"] = sources_preview

        if test_mode:
            LOG.info("Fuel scraper DEBUG snapshot: %s", summary)
        else:
            LOG.debug("Fuel scraper snapshot: %s", summary)

    except Exception:
        LOG.exception("Failed while logging fuel scraper output")

async def check_and_post_fuel_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Daily fuel & LPG update job - posts only if petrol OR LPG price changed since last post.
    Uses persistent channel_snapshots (reuses existing 'last_posted_price' for petrol + 'raw' JSON for LPG previous).
    When TEST_MODE is truthy, skip time-window, recency, and change checks (always attempt/post).
    """
    now = datetime.now(TIMEZONE)
    FUEL_TRACKING_KEY = "https://fuelpricewatch.com/nigeria"

    # Wake-up window: only run between 07:00-07:59 local TIMEZONE
    if not TEST_MODE:
        if not (7 <= now.hour < 8):
            LOG.debug("Outside of 7 AM window — skipping fuel update")
            return
    else:
        LOG.debug("TEST_MODE enabled: skipping time window check")

    # Load last snapshot (uses existing load_channel_snapshot)
    snapshot = None
    if not TEST_MODE:
        try:
            snapshot = await load_channel_snapshot(FUEL_TRACKING_KEY)
        except Exception:
            LOG.exception("Failed to load channel snapshot")

        # Recency check: prevent duplicate posts within ~20 hours
        if snapshot and snapshot.get("last_posted_at"):
            try:
                last_run = datetime.fromisoformat(snapshot["last_posted_at"])
                if (now - last_run).total_seconds() < 72_000:
                    LOG.info("Fuel update already sent recently at %s — skipping", last_run.isoformat())
                    return
            except Exception:
                LOG.warning("Could not parse last_posted_at; proceeding")
    else:
        LOG.debug("TEST_MODE enabled: skipping snapshot recency check")

    # Extract previous prices from snapshot (compatible with existing schema)
    previous_petrol_raw = snapshot.get("last_posted_price") if snapshot else None
    previous_lpg_raw = None
    if snapshot and snapshot.get("raw"):
        try:
            previous_lpg_raw = snapshot["raw"].get("previous_lpg_per_kg")
        except Exception:
            pass

    # === Scrape Petrol (PMS) ===
    petrol_data = None
    try:
        scraper_fn = None
        if "scrape_fuel_prices_many" in globals() and callable(globals().get("scrape_fuel_prices_many")):
            scraper_fn = globals()["scrape_fuel_prices_many"]
        elif "scrape_fuel_prices" in globals() and callable(globals().get("scrape_fuel_prices")):
            scraper_fn = globals()["scrape_fuel_prices"]

        if scraper_fn:
            if asyncio.iscoroutinefunction(scraper_fn):
                petrol_data = await scraper_fn()
            else:
                loop = asyncio.get_event_loop()
                petrol_data = await loop.run_in_executor(None, scraper_fn)
            log_fuel_scraper_data(petrol_data, test_mode=TEST_MODE)
    except Exception:
        LOG.exception("Error scraping petrol prices")

    # === Scrape LPG ===
    lpg_data = None
    try:
        loop = asyncio.get_event_loop()
        lpg_data = await loop.run_in_executor(None, scrape_lpg_prices)
        LOG.info("LPG scrape completed: %s", lpg_data.get("retail_estimate_lagos", "N/A"))
    except Exception:
        LOG.exception("Error scraping LPG prices")

    if not petrol_data and not lpg_data:
        LOG.warning("Both scrapers failed — skipping post")
        return

    # Normalize petrol
    petrol_avg_formatted = None
    petrol_avg_raw = None
    petrol_changed = True

    if petrol_data:
        petrol_avg_formatted = petrol_data.get("avg_petrol")
        petrol_avg_raw = petrol_data.get("avg_raw")
        if isinstance(petrol_avg_formatted, (int, float)):
            petrol_avg_formatted = _format_naira(float(petrol_avg_formatted))

        if not petrol_avg_formatted or str(petrol_avg_formatted).strip().upper() in {"", "N/A", "NONE"}:
            petrol_avg_formatted = None

        if not TEST_MODE and previous_petrol_raw is not None and petrol_avg_raw is not None:
            petrol_changed = abs(petrol_avg_raw - previous_petrol_raw) >= 0.01

    # Normalize LPG
    lpg_retail_range = None
    lpg_depot_per_kg_raw = None
    lpg_changed = True

    if lpg_data and lpg_data.get("retail_estimate_lagos") != "N/A":
        lpg_retail_range = lpg_data["retail_estimate_lagos"]
        lpg_depot_per_kg_raw = lpg_data.get("depot_per_kg_raw")

        if not TEST_MODE and previous_lpg_raw is not None and lpg_depot_per_kg_raw is not None:
            lpg_changed = abs(lpg_depot_per_kg_raw - previous_lpg_raw) >= 0.01

    # Decide whether to post: if either price changed (or TEST_MODE or first run)
    should_post = TEST_MODE or petrol_changed or lpg_changed or (previous_petrol_raw is None and previous_lpg_raw is None)

    if not should_post:
        LOG.info(
            "No price change detected (Petrol: %.2f vs prev %.2f | LPG: %.2f vs prev %.2f) — skipping post",
            petrol_avg_raw or 0, previous_petrol_raw or 0,
            lpg_depot_per_kg_raw or 0, previous_lpg_raw or 0
        )
        return

    LOG.info("Price change detected — proceeding to post update")

    # === Build message ===
    message_lines = [
        "🌅 <b>Daily Fuel Price Report — Nigeria</b>",
        f"📅 {now.strftime('%B %d, %Y')} — <i>Morning update</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # Petrol section (if data available)
    if petrol_avg_formatted:
        change_parts = []
        change_absolute = petrol_data.get("change_absolute", "N/A")
        change_percent = petrol_data.get("change_percent", "N/A")
        if change_absolute and change_absolute != "N/A":
            change_parts.append(change_absolute)
        if change_percent and change_percent != "N/A":
            change_parts.append(f"({change_percent} from last period)")
        change_text = " ".join(change_parts) if change_parts else "No change data"

        change_emoji = "📊"
        if change_absolute != "N/A" and change_absolute.strip().startswith("+"):
            change_emoji = "📈"
        elif change_absolute != "N/A" and change_absolute.strip().startswith("-"):
            change_emoji = "📉"

        last_updated_petrol = petrol_data.get("last_updated", "Live data")

        # Sources processing
        sources = petrol_data.get("sources", [])
        reported = 0
        total_sources = max(1, len(sources))
        sources_lines = []
        for s in sources:
            if not isinstance(s, dict):
                if isinstance(s, str):
                    sources_lines.append(f"• {s}")
                continue
            url = s.get("source", "")
            if not url:
                continue
            src_name = "FuelPriceWatch Live App" if "app.fuelpricewatch.com" in url else "FuelPriceWatch"
            price_str = s.get("price_str")
            if isinstance(price_str, (int, float)):
                price_str = _format_naira(float(price_str))
            err = s.get("error")
            src_change = ""
            if s.get("change_percent") and s.get("change_percent") != "N/A":
                src_change += f" {s['change_percent']}"
            if s.get("change_absolute") and s.get("change_absolute") != "N/A":
                src_change += f" {s['change_absolute']}"
            if price_str and not err:
                reported += 1
                sources_lines.append(f"• <a href=\"{_safe_url(url)}\">{src_name}</a> — {price_str}{src_change}")
            else:
                err_label = err or "no data"
                sources_lines.append(f"• <a href=\"{_safe_url(url)}\">{src_name}</a> — {err_label}")

        confidence = f"{reported}/{total_sources}"
        if not sources_lines:
            sources_lines = [f"• <a href=\"https://app.fuelpricewatch.com/\">FuelPriceWatch</a> — {petrol_avg_formatted}"]
            confidence = "1/1"

        message_lines.extend([
            "⛽ <b>Petrol (PMS) — National Average</b>",
            f"   <b>Price:</b> {petrol_avg_formatted}",
            f"   {change_emoji} <b>Change today:</b> {change_text}",
            f"   🕒 <b>Last updated:</b> {last_updated_petrol}",
            f"   🔎 <b>Confidence:</b> {confidence}",
            "",
            "   🏷️ <b>Sources</b>",
        ])
        message_lines.extend([f"   {line}" for line in sources_lines])
        message_lines.extend([
            "",
            "<blockquote>",
            "Disclaimer: This is the <b>national average</b> PMS price reported by official sources. "
            "Actual pump prices may vary significantly by state, city, and station.",
            "</blockquote>",
            "",
        ])

    # LPG section (if data available)
    if lpg_retail_range:
        lpg_depot_avg = lpg_data.get("avg_depot_20mt", "N/A")
        lpg_depot_per_kg = lpg_data.get("avg_depot_per_kg", "N/A")
        lpg_last_updated = lpg_data.get("last_updated", "Today")
        lpg_note = lpg_data.get("note", "How we estimate Lagos retail: We take the average depot price per kg from major depots, then add ₦400–600/kg for typical additional costs (transport to stations, bottling/filling, dealer profit, and minor fees).")

        message_lines.extend([
            "🔥 <b>LPG (Cooking Gas) — Lagos Retail Estimate</b>",
            f"   📊 <b>Depot average (20MT):</b> {lpg_depot_avg}",
            f"      <b>Per kg at depot:</b> {lpg_depot_per_kg}",
            f"   🏙️ <b>Estimated retail:</b> {lpg_retail_range}",
            f"   🕒 <b>Data from:</b> {lpg_last_updated} — <a href=\"https://lpginnigeria.com/chart\">LPG NIGERIA CHART 📊</a>",
            "",
            "<blockquote>",
            "Disclaimer: This is an <b>estimated Lagos retail price</b> based on current depot averages + typical ₦400–600/kg markup "
            "(transport, bottling, dealer margin, etc.). Actual prices vary by station and location.",
            "</blockquote>",
            "",
            f"<i>{lpg_note}</i>",
            "",
        ])

    message_lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "<i>Tip: tap source links for live bulletins.</i> 🔗",
    ])

    message = "\n".join(message_lines)

    # Send
    try:
        sent_results = await safe_send(
            context.bot,
            CHANNEL_DEAL_CHAT_ID,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        LOG.exception("Failed to send update")
        sent_results = []

    any_success = any(res[1] for res in sent_results if isinstance(res, (list, tuple)) and len(res) > 1)

    if any_success and not TEST_MODE:
        try:
            # Prepare snapshot dict compatible with existing save_channel_snapshot
            snapshot_to_save = {
                "last_posted_at": now.isoformat(),
                "last_posted_price": petrol_avg_raw,  # Reuses existing column for petrol
            }

            # Merge LPG previous into raw JSONB
            existing_raw = snapshot.get("raw", {}) if snapshot else {}
            new_raw = {
                **existing_raw,
                "previous_lpg_per_kg": lpg_depot_per_kg_raw,
                "sources_confidence": confidence if petrol_avg_formatted else "N/A",
            }
            snapshot_to_save["raw"] = new_raw  # Will be JSON dumped in DB function

            await save_channel_snapshot(
                FUEL_TRACKING_KEY,
                snapshot_to_save,
                expires_hours=72,
            )
            LOG.info("Update posted and snapshot saved (petrol: %.2f → last_posted_price, lpg: %.2f → raw.previous_lpg_per_kg)",
                     petrol_avg_raw or 0, lpg_depot_per_kg_raw or 0)
        except Exception:
            LOG.exception("Failed to save channel snapshot")
    else:
        if TEST_MODE:
            LOG.debug("TEST_MODE: skipping snapshot save")
        else:
            LOG.warning("No successful sends; snapshot not updated.")

def generate_source_report(
    name: str,
    items: List[Dict[str, Any]],
    gen_time: str,
    main_url: str,
) -> str:
    """
    Generates a clean HTML message for a single source.
    """
    lines = [
        f"<b>{name}</b>",
        f"<i>Generated: {gen_time} (WAT)</i>",
        "━━━━━━━━━━━━━━━━━━",
        "",
    ]

    if not items:
        lines.append("<i>No recent updates detected</i>")
    else:
        for item in items:
            title_line = f"• <b>{item['title']}</b>"
            if item.get("date"):
                title_line += f" — <i>{item['date']}</i>"
            lines.append(title_line)

            snippet_text = item.get("snippet", "").strip()
            if item.get("pdf"):
                size_str = f" ({item.get('pdf_size_kb')} KB)" if item.get("pdf_size_kb") else ""
                link_text = f"<a href=\"{item.get('pdf_url') or item['link']}\">Download PDF{size_str}</a>"
                if not snippet_text:
                    snippet_text = "PDF Document"
            else:
                link_text = f"<a href=\"{item['link']}\">View Details</a>"

            snippet_text = snippet_text.replace("Read More", "").replace("View Details", "").strip()

            if snippet_text:
                lines.append(f"  └─ {snippet_text} → {link_text}")
            else:
                lines.append(f"  └─ {link_text}")

        lines.append("")
        lines.append(f"<b>Updates found: {len(items)}</b>")

    lines.append("")
    lines.append(f"🔗 <a href=\"{main_url}\">Visit {name} website</a>")
    lines.append("")
    lines.append("<i>Always verify on the official website before acting.</i>")

    return "\n".join(lines)

async def check_and_post_school_updates(context: ContextTypes.DEFAULT_TYPE):
    """
    Job: Scrapes school sources (grouped), extracts updates per source,
    and posts separate messages for sources with new/changed content.
    Respects MAX_CHANNEL_POSTS_PER_RUN and sends one message per source.
    """
    global TEST_MODE
    force_mode = bool(TEST_MODE or SCHOOL_FORCE_POST)

    LOG.info("--- SCHOOL UPDATES JOB STARTED ---")

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.info("Channel posting disabled — skipping school updates")
        return

    max_posts = MAX_CHANNEL_POSTS_PER_RUN or 5
    send_delay = 15
    eligible_candidates = []
    now = datetime.now(TIMEZONE)
    now_iso = now.isoformat()
    gen_time_str = now.strftime('%b %d, %Y — %H:%M')

    # --- PHASE 1: SCRAPE & EXTRACT PER GROUP ---
    for group_key, urls in DEFAULT_SCHOOL_SOURCES.items():
        if not urls:
            continue

        items: List[Dict[str, Any]] = []
        seen = set()  # dedupe across all URLs in the group

        for url in urls:
            try:
                html = await asyncio.get_event_loop().run_in_executor(None, _fetch_html, url)
                if not html:
                    continue
                soup = BeautifulSoup(html, "lxml")
                source_items = extract_anchors_from_soup(soup, url, seen=seen)
                items.extend(source_items)
            except Exception as e:
                LOG.warning("Scrape/extract failed for %s (%s): %s", url, group_key, e)

        if not items:
            LOG.info("No items extracted for group '%s'", group_key)
            continue

        # Limit per source + sort (dates first, then longer snippets)
        items = items[:20]
        items.sort(key=lambda x: (x.get('date') is None, -len(x.get('snippet') or "")))

        # Generate report text for this source
        main_url = urls[0]  # primary URL for footer link
        report_text = generate_source_report(group_key, items, gen_time_str, main_url)

        # Hash for change detection
        current_hash = _hash_report_content(report_text)

        # Per-source snapshot key
        snapshot_key = f"school_updates_{_slugify(group_key)}"
        snapshot = None
        try:
            snapshot = await load_channel_snapshot(snapshot_key)
        except Exception:
            LOG.exception("Failed loading snapshot for %s", snapshot_key)

        previous_hash = snapshot.get("content_hash") if snapshot else None
        last_posted_at_str = snapshot.get("last_posted_at") if snapshot else None

        is_new = previous_hash is None
        content_changed = current_hash != previous_hash

        # Recency check (per source)
        recency_ok = True
        if not force_mode and last_posted_at_str:
            try:
                last_dt = datetime.fromisoformat(last_posted_at_str)
                hours_since = (now - last_dt).total_seconds() / 3600
                if hours_since < 4:  # minimum 4 hours between posts for same source
                    recency_ok = False
                    LOG.info("Skipping %s — posted %.1f hours ago", group_key, hours_since)
            except Exception:
                pass

        # Eligibility
        should_post = force_mode or (len(items) > 0 and (is_new or content_changed) and recency_ok)

        if should_post:
            eligible_candidates.append({
                "group_key": group_key,
                "report_text": report_text,
                "stats": {
                    "item_count": len(items),
                    "is_new": is_new,
                    "content_changed": content_changed,
                },
                "snapshot_key": snapshot_key,
                "current_hash": current_hash,
            })

    # --- PHASE 2: PRIORITIZE & POST ---
    if not eligible_candidates:
        LOG.info("No eligible school sources to post.")
        return

    # Prioritize: most updates first
    eligible_candidates.sort(key=lambda x: -x["stats"]["item_count"])

    to_post = eligible_candidates[:max_posts]

    posted_count = 0
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

    for item in to_post:
        emoji = "🆕" if item["stats"]["is_new"] else "🔄"
        message = f"{emoji} <b>School Updates</b>\n━━━━━━━━━━━━━━━━━━\n\n{item['report_text']}"

        sent_successfully = False
        for chat_id in targets:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                sent_successfully = True
                LOG.info("Posted updates for '%s' to %s", item["group_key"], chat_id)
            except Exception as e:
                LOG.error("Failed posting '%s' to %s: %s", item["group_key"], chat_id, e)

        if sent_successfully:
            try:
                await save_channel_snapshot(
                    item["snapshot_key"],
                    {
                        "last_posted_at": now_iso,
                        "content_hash": item["current_hash"],
                        "item_count": item["stats"]["item_count"],
                    },
                    expires_hours=168,
                )
            except Exception:
                LOG.exception("Failed saving snapshot for %s", item["snapshot_key"])

            posted_count += 1
            if posted_count < len(to_post):
                await asyncio.sleep(send_delay)

    LOG.info("--- SCHOOL JOB FINISHED: %d sources posted ---", posted_count)

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
    max_retries = 8
    for i in range(max_retries):
        acquired = await lock.acquire(blocking=False)
        if acquired:
            break
        if i > 5:
            LOG.warning(f"Lock taken, retry {i+1}/{max_retries} in 5s...")
        await asyncio.sleep(5)
    else:
        # If we exhausted retries
        LOG.warning("Stealing lock: Old instance failed to release within 40s.")
        await r.delete(lock_name)
        await asyncio.sleep(0.5)
        acquired = await lock.acquire(blocking=False)
        if not acquired:
            LOG.error("Critical: Could not acquire lock even after stealing.")
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
            application.job_queue.run_daily(
                callback=check_and_post_fuel_prices,
                #interval=600,
                time=time(hour=7, minute=0, second=0, tzinfo=TIMEZONE),
                name="check_fuel_prices",
                #first=10,
            )
            application.job_queue.run_repeating(
                callback=check_trials,
                interval=86400,
                first=3600,
                name="trial_checker"
            )
            application.job_queue.run_repeating(
                callback=check_and_post_school_updates,
                #time=std_time(hour=7, minute=0, second=0, tzinfo=TIMEZONE),
                interval=120,  # Convert hours to seconds
                first=10,  # Start 1 minute after bot launch
                name="school_updates_poster"
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
            try:
               await renewal_task # wait for cancellation to finish
            except asyncio.CancelledError:
                pass
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