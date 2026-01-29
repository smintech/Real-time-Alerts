# bot/scheduler.py - UPDATED FOR DUAL-LAYER PERSISTENCE

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
import re 
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
    get_domain_from_url,
    scrape_fuel_prices,
    scrape_lpg_prices,
    extract_school_news_listings,
    fetch_article_details,
    scrape_school_news,
    safe_send,  # Import from utils
)

from bot.persistence import (
    load_last_snapshot,
    save_snapshot,
    load_channel_snapshot,
    save_channel_snapshot,
    check_duplicate_post,
    mark_as_posted,
    compute_content_hash,
    delete_expired_channel_snapshots,
    delete_expired_post_history,
    cleanup_all_expired,
    wipe_channel_snapshots_redis,
    initialize_database,
)

from Utils.format import format_telegram_alert, _safe_currency, update_exchange_rate

logging.basicConfig(level=logging.INFO)
LOG = logging.getLogger(__name__)

application: Application | None = None
nest_asyncio.apply()
TEST_MODE = os.getenv("TEST_MODE", "false").lower() in ("1", "true", "yes")
SCHOOL_FORCE_POST = os.getenv("SCHOOL_UPDATES_FORCE", "false").lower() in ("1", "true", "yes") 

# ═══════════════════════════════════════════════════════════════════════════
# HELPER FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

def _safe_url(u: str) -> str:
    """Simple safety wrapper for URLs in HTML links."""
    if not u:
        return ""
    u = u.strip()
    if u.startswith(("http://", "https://")):
        return u
    return f"https://{u}"

def _slugify(text: str) -> str:
    """Simple slugify for snapshot keys."""
    return re.sub(r'[^a-z0-9_]', '', text.lower().replace(' ', '_').replace('-', '_'))

def _format_naira(amount: Optional[float]) -> str:
    """Format naira amount with proper formatting."""
    if amount is None:
        return "N/A"
    try:
        return f"₦{int(round(amount)):,}"
    except:
        return "N/A"

# ═══════════════════════════════════════════════════════════════════════════
# WATCH MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

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

    for user_id, watches in list(user_watches.items()):
        try:
            user_settings_for_id = user_settings.get(user_id, {})
            enabled_cats = user_settings_for_id.get("enabled_categories", CATEGORIES.copy())

            for watch in list(watches):
                try:
                    if watch.get("status") != "active":
                        continue

                    watch_category = watch.get("category")
                    if watch_category and watch_category not in enabled_cats:
                        continue

                    now = datetime.now(TIMEZONE)
                    next_check = watch.get("next_check")
                    if next_check and now < next_check:
                        continue

                    # Scrape product
                    try:
                        new_data = await scrape_product(watch["url"])
                    except Exception as exc:
                        await handle_watch_failure(context, user_id, watch, exc=exc, reason="scrape_error")
                        continue

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

                    watch["fail_count"] = 0
                    watch.pop("next_check", None)

                    # Load previous snapshot
                    try:
                        prev_snapshot = await load_last_snapshot(watch["url"])
                    except Exception:
                        LOG.exception("Failed loading last snapshot for %s", watch.get("url"))
                        prev_snapshot = None

                    old_price = None
                    old_stock = watch.get("last_stock", "available")

                    if prev_snapshot:
                        old_price = prev_snapshot.get("current_price")
                        old_stock = prev_snapshot.get("stock_status", old_stock)

                    current_price = new_data["current_price"]

                    # First time seeing this product - save snapshot and continue
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

                        watch["last_price"] = current_price
                        watch["last_stock"] = new_data.get("stock_status")
                        watch["last_checked_at"] = datetime.now(TIMEZONE).isoformat()
                        continue

                    # Compute changes
                    changes = compute_changes(
                        {"current_price": old_price, "stock_status": old_stock},
                        {"current_price": current_price, "stock_status": new_data.get("stock_status", "available")}
                    )

                    if not changes.get("significant_change"):
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

                    price_diff_percent = changes.get("price_diff_percent", 0.0)
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

                    # Build and send alert
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

                    # Update watch
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

# ═══════════════════════════════════════════════════════════════════════════
# CHANNEL DEALS POSTING
# ═══════════════════════════════════════════════════════════════════════════

async def check_and_post_channel_deals(context: ContextTypes.DEFAULT_TYPE):
    """
    Job: Scrapes monitored URLs, groups them, finds the best deal,
    and posts to Telegram channels if a drop is detected or if it's a new item.
    Uses dual-layer persistence with deduplication.
    """
    start_time = std_time.time()
    LOG.info("═══════════════════════════════════════════════════════════")
    LOG.info("🔍 CHANNEL DEALS JOB STARTED")
    LOG.info("═══════════════════════════════════════════════════════════")

    await update_exchange_rate()

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.info("Channel posting disabled or CHANNEL_DEAL_CHAT_ID missing — skipping")
        return

    max_posts = MAX_CHANNEL_POSTS_PER_RUN or 5
    send_delay = 15
    eligible_candidates = []
    now = datetime.now(timezone.utc)

    # --- PHASE 1: SCRAPE EVERYTHING ---
    LOG.info("\n📦 PHASE 1: Scraping %d product groups...", len(CHANNEL_MONITORED_URLS))
    
    for group_key, urls in CHANNEL_MONITORED_URLS.items():
        LOG.info("\n  ┌─ Processing group: '%s' (%d URLs)", group_key, len(urls))
        entries = []
        
        for idx, url in enumerate(urls, 1):
            LOG.info("    ├─ [%d/%d] Attempting: %s", idx, len(urls), url[:80] + "..." if len(url) > 80 else url)
            
            try:
                # Scrape with timeout
                data = await asyncio.wait_for(scrape_product(url), timeout=90.0)
                
                if not data:
                    LOG.warning("    │   ✗ No data returned")
                    continue
                
                current_price = data.get("current_price")
                if current_price is None:
                    LOG.warning("    │   ✗ No price found (status: %s)", data.get("stock_status", "unknown"))
                    continue
                
                entries.append({"url": url, "data": data})
                LOG.info("    │   ✓ Success: ₦%s - %s", 
                        f"{current_price:,.0f}",
                        data.get("title", "Unknown")[:60])
                
            except asyncio.TimeoutError:
                LOG.error("    │   ✗ TIMEOUT after 90s")
                continue
                
            except Exception as e:
                LOG.error("    │   ✗ Exception: %s", str(e)[:100])
                LOG.exception("    │      Full trace:")
                continue
            
            finally:
                # Small delay between URLs in same group to avoid rate limiting
                if idx < len(urls):
                    await asyncio.sleep(2)
        
        if not entries:
            LOG.warning("  └─ ⚠️  NO SUCCESSFUL SCRAPES for '%s' - SKIPPING GROUP", group_key)
            continue
        
        LOG.info("  └─ ✓ Scraped %d/%d URLs successfully for '%s'", len(entries), len(urls), group_key)

        # --- PHASE 2: DETERMINE BEST PRICE & HISTORY ---
        try:
            best_entry = min(entries, key=lambda e: float(e["data"]["current_price"]))
            LOG.info("       → Best price: ₦%s at %s", 
                    f"{best_entry['data']['current_price']:,.0f}",
                    best_entry['data'].get('site', 'unknown'))
        except Exception as e:
            LOG.error("  └─ ✗ Could not determine best price: %s", e)
            continue

        current_price = float(best_entry["data"]["current_price"])
        
        # Prepare snapshot data for dedup check
        snapshot_data = {
            "title": best_entry["data"].get("title", group_key),
            "current_price": current_price,
            "site": best_entry["data"].get("site"),
            "url": best_entry["url"],
            "raw": best_entry["data"].get("raw", {}),
        }
        
        # Compute content hash for deduplication
        content_hash = compute_content_hash(snapshot_data)
        
        # Load history for this URL
        try:
            history = await load_channel_snapshot(best_entry["url"])
        except Exception as e:
            LOG.warning("       ⚠️  Failed to load snapshot: %s", str(e)[:50])
            history = None
            
        last_posted_price = history.get("last_posted_price") if history else None
        last_posted_at = history.get("last_posted_at") if history else None
        last_content_hash = history.get("content_hash") if history else None

        is_crypto = "binance" in (best_entry["data"].get("site", "").lower()) or "SYMBOL:" in best_entry["url"]
        is_new = last_posted_price is None

        # --- IMPROVED DUPLICATE DETECTION ---
        if content_hash == last_content_hash and not TEST_MODE:
            LOG.debug("       ℹ️  Content hash matches last post")
            
            if is_new:
                LOG.info("       → New item - posting despite hash match")
            elif last_posted_at:
                try:
                    last_dt = datetime.fromisoformat(last_posted_at.replace("Z", "+00:00") if "Z" in last_posted_at else last_posted_at)
                    hours_since = (now - last_dt).total_seconds() / 3600
                    
                    min_hours = 24 if is_crypto else 48
                    
                    if hours_since < min_hours:
                        LOG.info("       ⏭️  SKIPPING: posted %.1fh ago (need %dh minimum)", 
                                hours_since, min_hours)
                        continue
                    else:
                        LOG.info("       → Hash matches but %dh passed - re-checking", int(hours_since))
                except Exception as e:
                    LOG.warning("       ⚠️  Error parsing timestamp: %s", e)

        # --- PHASE 3: ELIGIBILITY CHECK ---
        ref_price = last_posted_price if last_posted_price is not None else current_price
        price_change = round(((current_price - ref_price) / ref_price) * 100, 1) if ref_price != 0 else 0.0
        abs_change = abs(price_change)
        drop_pct = round(((ref_price - current_price) / ref_price) * 100, 1) if ref_price > current_price else 0.0
        savings = max(ref_price - current_price, 0)

        LOG.info("       💰 Current: ₦%s | Last: %s", 
                f"{current_price:,.0f}", 
                f"₦{ref_price:,.0f}" if last_posted_price else "NEW")
        LOG.info("       📊 Change: %+.1f%% | Drop: %.1f%% | Savings: ₦%s",
                price_change, drop_pct, f"{savings:,.0f}")

        should_post = is_new or TEST_MODE

        if not is_new and not TEST_MODE:
            if abs_change > 0:
                if is_crypto:
                    if abs_change >= 1.0:
                        if last_posted_at:
                            try:
                                last_dt = datetime.fromisoformat(last_posted_at.replace("Z", "+00:00") if "Z" in last_posted_at else last_posted_at)
                                hours_since = (now - last_dt).total_seconds() / 3600
                                if hours_since >= 6:
                                    should_post = True
                                    LOG.info("       ✓ Crypto: %.1f%% change + %dh elapsed", abs_change, int(hours_since))
                                else:
                                    LOG.info("       ⏭️  Crypto: %.1f%% change but only %.1fh elapsed", abs_change, hours_since)
                            except:
                                should_post = True
                        else:
                            should_post = True
                    else:
                        LOG.info("       ⏭️  Crypto change too small: %.1f%% < 1.0%%", abs_change)
                else:
                    if price_change < 0 and -price_change >= MIN_DROP_PERCENT_FOR_CHANNEL and savings >= MIN_SAVINGS_FOR_CHANNEL:
                        should_post = True
                        LOG.info("       ✓ Drop qualifies: %.1f%% + ₦%s savings", -price_change, f"{savings:,.0f}")
                    else:
                        LOG.info("       ⏭️  Drop insufficient: %.1f%% or ₦%s savings (need %.1f%% + ₦%s)",
                                -price_change if price_change < 0 else 0, 
                                f"{savings:,.0f}",
                                MIN_DROP_PERCENT_FOR_CHANNEL,
                                f"{MIN_SAVINGS_FOR_CHANNEL:,.0f}")
        
        if should_post:
            eligible_candidates.append({
                "group_key": group_key,
                "best_entry": best_entry,
                "entries": entries,
                "content_hash": content_hash,
                "stats": {
                    "drop_pct": drop_pct,
                    "change_pct": price_change,
                    "is_new": is_new,
                    "is_crypto": is_crypto,
                    "savings": savings
                }
            })
            LOG.info("       ✅ ELIGIBLE for posting")
        else:
            LOG.info("       ⏭️  NOT eligible")

    # --- PHASE 4: PRIORITIZE & POST ---
    LOG.info("\n═══════════════════════════════════════════════════════════")
    LOG.info("📊 SUMMARY: %d eligible deals from %d groups", 
             len(eligible_candidates), len(CHANNEL_MONITORED_URLS))
    LOG.info("═══════════════════════════════════════════════════════════")
    
    if not eligible_candidates:
        LOG.info("  └─ No eligible deals to post")
        return

    eligible_candidates.sort(key=lambda x: (
        -x["stats"]["drop_pct"],
        -x["stats"]["savings"],
    ))
    to_post = eligible_candidates[:max_posts]

    LOG.info("\n📤 POSTING PHASE: Sending %d deals (max: %d)...", len(to_post), max_posts)

    posted_count = 0
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

    for idx, item in enumerate(to_post, 1):
        best_entry = item["best_entry"]
        stats = item["stats"]
        group_key = item["group_key"]

        LOG.info("\n  [%d/%d] Posting: %s", idx, len(to_post), group_key)

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
            except:
                p = 0.0
            site_label = e["data"].get("site", get_domain_from_url(e["url"])).upper()
            rel_pct = round(((p - best_price_val) / best_price_val) * 100, 1) if best_price_val > 0 else 0.0
            mark = "✅ BEST" if p == best_price_val else ("⚠️ Good" if rel_pct <= 5.0 else "•")
            price_str = _safe_currency(p, site=site_label) if p else "N/A"
            comparison_lines.append(f"{mark} <a href=\"{e['url']}\">{site_label}</a>: {price_str}")

        comparison_text = "🏪 <b>Comparison:</b>\n" + "\n".join(comparison_lines) if comparison_lines else ""

        # Construct Caption
        if stats["is_crypto"]:
            header = "🆕 NEW CRYPTO TRACKED" if stats["is_new"] else f"{'📈' if stats['change_pct'] > 0 else '📉'} NAIRA STRENGTH"
            caption = (
                f"<b>{header}</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"💰 Current: {_safe_currency(price, site=best_entry['data'].get('site', 'unknown'))}\n"
                f"📊 Change: {stats['change_pct']:+.1f}%\n\n"
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

        if description:
            max_desc_len = 1024 - len(caption) - 50
            if max_desc_len > 20:
                truncated = description[:max_desc_len].rstrip() + "..." if len(description) > max_desc_len else description
                caption += f"\n\n📄 <b>Product details:</b>\n<blockquote>{truncated}</blockquote>"

        caption += "\n\n🔔 @Real_Time_Alert"

        # Send to targets
        sent_successfully = False
        for chat_id in targets:
            try:
                if image:
                    await context.bot.send_photo(chat_id=chat_id, photo=image, caption=caption, parse_mode="HTML")
                else:
                    await context.bot.send_message(chat_id=chat_id, text=caption, parse_mode="HTML", disable_web_page_preview=True)
                sent_successfully = True
                LOG.info("        ✓ Posted to %s", chat_id)
            except Exception as e:
                LOG.error("        ✗ Failed to post to %s: %s", chat_id, str(e)[:60])

        # Update snapshot
        if sent_successfully:
            try:
                snapshot_to_save = {
                    "site": best_entry["data"].get("site"),
                    "title": title,
                    "url": best_entry["url"],
                    "current_price": price,
                    "content_hash": item["content_hash"],
                    "raw": best_entry["data"].get("raw", {}),
                }
                await mark_as_posted(best_entry["url"], snapshot_to_save)
                LOG.info("        💾 Snapshot saved")
            except Exception as e:
                LOG.error("        ⚠️  Failed to save snapshot: %s", str(e)[:60])
            
            posted_count += 1
            if posted_count < len(to_post):
                LOG.info("        ⏳ Waiting %ds...", send_delay)
                await asyncio.sleep(send_delay)

    elapsed = std_time.time() - start_time
    LOG.info("\n═══════════════════════════════════════════════════════════")
    LOG.info("✅ JOB COMPLETE: %d/%d deals posted in %.1fs", posted_count, len(to_post), elapsed)
    LOG.info("═══════════════════════════════════════════════════════════\n")

# ═══════════════════════════════════════════════════════════════════════════
# FUEL PRICES POSTING
# ═══════════════════════════════════════════════════════════════════════════

async def check_and_post_fuel_prices(context: ContextTypes.DEFAULT_TYPE):
    """
    Daily fuel & LPG update job - posts only if petrol OR LPG price changed.
    Uses dual-layer persistence with deduplication.
    """
    now = datetime.now(TIMEZONE)
    FUEL_TRACKING_KEY = "fuel_prices_nigeria"

    # Wake-up window check
    if not TEST_MODE:
        if not (7 <= now.hour < 8):
            LOG.debug("Outside of 7 AM window — skipping fuel update")
            return

    # Load last snapshot
    snapshot = None
    if not TEST_MODE:
        try:
            snapshot = await load_channel_snapshot(FUEL_TRACKING_KEY)
        except Exception:
            LOG.exception("Failed to load fuel snapshot")

        # Recency check
        if snapshot and snapshot.get("last_posted_at"):
            try:
                last_run = datetime.fromisoformat(snapshot["last_posted_at"])
                if (now - last_run).total_seconds() < 72_000:  # 20 hours
                    LOG.info("Fuel update already sent recently — skipping")
                    return
            except Exception:
                pass

    # Extract previous prices
    previous_petrol_raw = snapshot.get("last_posted_price") if snapshot else None
    previous_lpg_raw = None
    if snapshot and snapshot.get("raw"):
        try:
            previous_lpg_raw = snapshot["raw"].get("previous_lpg_per_kg")
        except Exception:
            pass

    # Scrape Petrol
    petrol_data = None
    try:
        petrol_data = await scrape_fuel_prices()
    except Exception:
        LOG.exception("Error scraping petrol prices")

    # Scrape LPG
    lpg_data = None
    try:
        lpg_data = await scrape_lpg_prices()
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

    # Decide whether to post
    should_post = TEST_MODE or petrol_changed or lpg_changed or (previous_petrol_raw is None)

    if not should_post:
        LOG.info("No price change detected — skipping fuel post")
        return

    # Build message
    message_lines = [
        "🌅 <b>Daily Fuel Price Report — Nigeria</b>",
        f"📅 {now.strftime('%B %d, %Y')} — <i>Morning update</i>",
        "━━━━━━━━━━━━━━━━━━",
    ]

    # Petrol section
    if petrol_avg_formatted:
        change_absolute = petrol_data.get("change_absolute", "N/A")
        change_percent = petrol_data.get("change_percent", "N/A")
        
        change_emoji = "📊"
        if change_absolute and str(change_absolute).startswith("+"):
            change_emoji = "📈"
        elif change_absolute and str(change_absolute).startswith("-"):
            change_emoji = "📉"

        message_lines.extend([
            "⛽ <b>Petrol (PMS) — National Average</b>",
            f"   <b>Price:</b> {petrol_avg_formatted}",
            f"   {change_emoji} <b>Change:</b> {change_absolute} ({change_percent})",
            "",
        ])

    # LPG section
    if lpg_retail_range:
        lpg_depot_avg = lpg_data.get("avg_depot_20mt", "N/A")
        lpg_depot_per_kg = lpg_data.get("avg_depot_per_kg", "N/A")
        
        message_lines.extend([
            "🔥 <b>LPG (Cooking Gas) — Lagos Retail Estimate</b>",
            f"   📊 <b>Depot average (20MT):</b> {lpg_depot_avg}",
            f"      <b>Per kg at depot:</b> {lpg_depot_per_kg}",
            f"   🏙️ <b>Estimated retail:</b> {lpg_retail_range}",
            "",
        ])

    message_lines.extend([
        "━━━━━━━━━━━━━━━━━━",
        "<i>Tap source links for live updates</i> 🔗",
    ])

    message = "\n".join(message_lines)

    # Send
    sent_successfully = False
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]
    
    for chat_id in targets:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            sent_successfully = True
        except Exception:
            LOG.exception("Failed to send fuel update")

    # Update snapshot
    if sent_successfully and not TEST_MODE:
        try:
            snapshot_data = {
                "last_posted_price": petrol_avg_raw,
                "raw": {
                    "previous_lpg_per_kg": lpg_depot_per_kg_raw,
                },
            }
            await mark_as_posted(FUEL_TRACKING_KEY, snapshot_data)
            LOG.info("Fuel prices posted and snapshot saved")
        except Exception:
            LOG.exception("Failed to save fuel snapshot")

# ═══════════════════════════════════════════════════════════════════════════
# SCHOOL UPDATES POSTING
# ═══════════════════════════════════════════════════════════════════════════

# bot/scheduler.py - FIXES FOR FORCE MODE & JAMB SCRAPING

async def check_and_post_school_updates(context: ContextTypes.DEFAULT_TYPE):
    """
    Job: Scrapes school sources, extracts updates per source,
    and posts separate messages for sources with new/changed content.
    Uses dual-layer persistence with deduplication.
    
    FIXED: Force mode now properly bypasses recency checks
    """
    LOG.info("--- SCHOOL UPDATES JOB STARTED ---")

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.info("Channel posting disabled — skipping school updates")
        return

    max_posts = MAX_CHANNEL_POSTS_PER_RUN or 5
    send_delay = 15
    eligible_candidates = []
    now = datetime.now(TIMEZONE)
    
    # Check if force mode is enabled
    force_mode = TEST_MODE or SCHOOL_FORCE_POST
    if force_mode:
        LOG.warning("🔴 FORCE MODE ENABLED - Bypassing recency and duplicate checks")

    # Scrape all sources
    for source_name, urls in DEFAULT_SCHOOL_SOURCES.items():
        if not urls:
            continue

        try:
            LOG.info(f"\n{'='*70}")
            LOG.info(f"Processing: {source_name}")
            LOG.info(f"{'='*70}")
            
            # Use the improved scrape_school_news function
            items = await scrape_school_news(
                urls,
                fetch_full_content=False,  # Just get listings with improved snippets
                max_articles=10
            )
            
            if not items:
                LOG.info("No items for %s", source_name)
                continue
            
            LOG.info(f"Found {len(items)} items for {source_name}")

            # Generate report
            report_lines = [
                f"<b>{source_name}</b>",
                f"<i>Updated: {now.strftime('%b %d, %Y — %H:%M WAT')}</i>",
                "━━━━━━━━━━━━━━━━━━",
                "",
            ]

            for item in items[:10]:  # Limit to 10 per source
                title_line = f"• <b>{item['title']}</b>"
                if item.get("date"):
                    title_line += f" — <i>{item['date']}</i>"
                report_lines.append(title_line)

                snippet = item.get("snippet", "").strip()
                if snippet and len(snippet) > 30:
                    # Clean up snippet
                    snippet = snippet.replace("Read More", "").replace("Click to read", "").strip()
                    report_lines.append(f"  └─ {snippet[:200]}... <a href=\"{item['link']}\">Read more</a>")
                else:
                    report_lines.append(f"  └─ <a href=\"{item['link']}\">View update</a>")

            report_lines.extend([
                "",
                f"<b>Updates found: {len(items)}</b>",
                "",
                f"🔗 <a href=\"{urls[0]}\">Visit {source_name}</a>",
            ])

            report_text = "\n".join(report_lines)
            
            # Compute hash for dedup
            content_hash = compute_content_hash({
                "title": source_name,
                "item_count": len(items),
                "raw": {"items": items[:5]}  # Use first 5 for hash
            })

            # Check if duplicate (SKIP IF FORCE MODE)
            snapshot_key = f"school_{_slugify(source_name)}"
            
            if not force_mode:
                is_duplicate = await check_duplicate_post(
                    ref=snapshot_key,
                    content_hash=content_hash,
                    lookback_hours=24
                )

                if is_duplicate:
                    LOG.info("Skipping duplicate school update for %s", source_name)
                    continue

            # Check recency (SKIP IF FORCE MODE)
            if not force_mode:
                snapshot = await load_channel_snapshot(snapshot_key)
                if snapshot and snapshot.get("last_posted_at"):
                    try:
                        last_dt = datetime.fromisoformat(snapshot["last_posted_at"])
                        hours_since = (now - last_dt).total_seconds() / 3600
                        if hours_since < 12:  # Minimum 12 hours between posts
                            LOG.info("Skipping %s — posted %.1f hours ago", source_name, hours_since)
                            continue
                    except Exception:
                        pass

            eligible_candidates.append({
                "source_name": source_name,
                "report_text": report_text,
                "content_hash": content_hash,
                "snapshot_key": snapshot_key,
                "item_count": len(items),
            })

        except Exception:
            LOG.exception("Failed to process %s", source_name)

    if not eligible_candidates:
        LOG.info("No eligible school sources to post")
        return

    # Sort by most items
    eligible_candidates.sort(key=lambda x: -x["item_count"])
    to_post = eligible_candidates[:max_posts]

    posted_count = 0
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

    for item in to_post:
        emoji = "🔴" if force_mode else ("🆕" if item.get("is_new") else "🔄")
        message = f"{emoji} <b>School Updates — {item['source_name']}</b>\n━━━━━━━━━━━━━━━━━━\n\n{item['report_text']}"

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
                LOG.info("Posted school update: %s", item['source_name'])
            except Exception as e:
                LOG.error("Failed posting school update: %s", e)

        if sent_successfully:
            # Don't save snapshot in force mode (to allow re-posting)
            if not force_mode:
                snapshot_data = {
                    "content_hash": item["content_hash"],
                    "item_count": item["item_count"],
                    "site": item["source_name"],
                    "title": f"School Updates - {item['source_name']}",
                }
                await mark_as_posted(item["snapshot_key"], snapshot_data)
            else:
                LOG.info("Force mode: Skipping snapshot save for re-posting capability")
            
            posted_count += 1
            if posted_count < len(to_post):
                await asyncio.sleep(send_delay)

    LOG.info("--- SCHOOL JOB FINISHED: %d sources posted ---", posted_count)

# ═══════════════════════════════════════════════════════════════════════════
# TRIAL MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════

async def check_trials(context: ContextTypes.DEFAULT_TYPE):
    """Validate trials and downgrade users whose trial expired."""
    bot = context.bot
    now = std_time.time()

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

                    msg = f"⚠️ Your {tier.capitalize()} trial expired. Downgraded to free tier."
                    try:
                        await safe_send(bot, user_id, msg)
                    except Exception:
                        LOG.exception("Failed to notify user about trial expiry")

                    watches = user_watches.get(user_id, [])
                    if len(watches) > MAX_WATCHES_FREE:
                        extra = len(watches) - MAX_WATCHES_FREE
                        msg2 = f"\nPlease remove {extra} watches to comply with the free tier limit."
                        try:
                            await safe_send(bot, user_id, msg2)
                        except Exception:
                            LOG.exception("Failed to notify about watch limit")
        except Exception:
            LOG.exception("Error checking trials for user %s", user_id)

# ═══════════════════════════════════════════════════════════════════════════
# REDIS LOCK FOR SINGLE INSTANCE
# ═══════════════════════════════════════════════════════════════════════════

async def acquire_long_running_lock(r: redis_async.Redis, lock_name: str = "telegram-bot-single-instance"):
    """Acquires & auto-renews an async Redis lock."""
    lock = r.lock(lock_name, timeout=10)

    max_retries = 8
    for i in range(max_retries):
        acquired = await lock.acquire(blocking=False)
        if acquired:
            break
        if i > 5:
            LOG.warning(f"Lock taken, retry {i+1}/{max_retries} in 5s...")
        await asyncio.sleep(5)
    else:
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
                await asyncio.sleep(4)
                if await lock.owned():
                    await lock.extend(10)
                    LOG.debug("Async lock renewed")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            LOG.warning("Async lock renewal failed: %s", e)

    renewal_task = asyncio.create_task(renew_loop())
    return True, (lock, renewal_task)

# ═══════════════════════════════════════════════════════════════════════════
# ERROR HANDLER
# ═══════════════════════════════════════════════════════════════════════════

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    LOG.exception("Unhandled handler error: %s", context.error)

# ═══════════════════════════════════════════════════════════════════════════
# MAIN BOT RUNNER
# ═══════════════════════════════════════════════════════════════════════════

async def run_bot():
    """Main bot runner with PTB v20+/v21 compatibility."""
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
        # Initialize database
        initialize_database()
        
        # Connect to Redis
        r = await redis_async.from_url(REDIS_URL, decode_responses=True)

        # Acquire lock
        lock_acquired, lock_info = await acquire_long_running_lock(r)
        if not lock_acquired:
            LOG.warning("Could not acquire lock → exiting")
            return

        lock, renewal_task = lock_info

        # Build application
        LOG.info("Building Telegram Application...")
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        await application.initialize()

        # Add handlers
        for handler in get_application_handlers():
            application.add_handler(handler)
        application.add_error_handler(global_error_handler)
        
        # Optional: wipe channel snapshots on startup
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
                time=time(hour=7, minute=0, second=0, tzinfo=TIMEZONE),
                name="check_fuel_prices",
            )
            application.job_queue.run_repeating(
                callback=check_trials,
                interval=86400,
                first=3600,
                name="trial_checker"
            )
            application.job_queue.run_repeating(
                callback=check_and_post_school_updates,
                interval=300,  # 6 hours
                first=30,
                #time=time(hour=7, minute=0, second=0, tzinfo=TIMEZONE),
                name="school_updates_poster"
            )
            # Cleanup job
            application.job_queue.run_daily(
                callback=lambda ctx: cleanup_all_expired(),
                time=time(hour=3, minute=0, second=0, tzinfo=TIMEZONE),
                name="cleanup_expired"
            )

        # Clear webhook
        try:
            await application.bot.delete_webhook(drop_pending_updates=True)
            LOG.info("Webhook cleared")
        except Exception as e:
            LOG.debug("Webhook cleanup: %s", e)

        # Start polling
        LOG.info("Starting long-running polling...")
        await application.start()
        await application.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES
        )

        # Keep running
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        LOG.info("run_bot task cancelled — graceful shutdown")

    except Exception as exc:
        LOG.exception("Fatal error in run_bot: %s", exc)

    finally:
        LOG.info("Cleaning up resources...")

        if application:
            try:
                if application.updater:
                    await application.updater.stop()
                await application.stop()
                await application.shutdown()
            except Exception as e:
                LOG.warning("Application shutdown error: %s", e)

        if renewal_task:
            renewal_task.cancel()
            try:
                await renewal_task
            except asyncio.CancelledError:
                pass

        if lock:
            try:
                if await lock.owned():
                    await lock.release()
                    LOG.info("Redis lock released")
            except Exception as e:
                LOG.warning("Failed to release lock: %s", e)

        if r:
            await r.aclose()
            LOG.info("Redis connection closed")

    LOG.info("run_bot finished.")
