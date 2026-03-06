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
    scrape_school_news,
    safe_send,
    get_domain_from_url,
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
    record_posted_hash,
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
                data = await asyncio.wait_for(scrape_product(url), timeout=60.0)
                
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
                
                # Small delay between URLs in group
                if idx < len(urls):
                    await asyncio.sleep(2)
                    
            except asyncio.TimeoutError:
                LOG.error("    │   ✗ TIMEOUT after 60s")
                continue
                
            except Exception as e:
                LOG.error("    │   ✗ Exception: %s", str(e)[:100])
                LOG.exception("    │      Full trace:")
                continue  # Skip bad URL
        
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
    Includes source attribution and improved formatting.
    """
    now = datetime.now(TIMEZONE)
    current_weekday = now.weekday()
    current_day_name = now.strftime('%A')
    
    if not TEST_MODE:
        if current_weekday != 5:  # 5 = Saturday
            LOG.debug("⏭️  Skipping fuel price check — today is %s (%d), not Saturday",current_day_name,current_weekday)
            return
    
    LOG.info("🛢️  Running weekly fuel price check (Saturday %02d:%02d:%02d)",now.hour,now.minute,now.second)

    FUEL_TRACKING_KEY = "fuel_prices_nigeria"

    # Load last snapshot
    snapshot = None
    if not TEST_MODE:
        try:
            snapshot = await load_channel_snapshot(FUEL_TRACKING_KEY)
        except Exception:
            LOG.exception("Failed to load fuel snapshot")

        # --- FIXED RECENCY CHECK (handles both string and datetime) ---
        if snapshot and snapshot.get("last_posted_at"):
            try:
                last_val = snapshot["last_posted_at"]
                # Handle both string and datetime objects
                if isinstance(last_val, datetime):
                    last_dt = last_val
                else:
                    # Replace 'Z' with '+00:00' for fromisoformat compatibility
                    last_val_str = last_val.replace("Z", "+00:00") if "Z" in last_val else last_val
                    last_dt = datetime.fromisoformat(last_val_str)
                
                # Ensure timezone awareness (convert to UTC for comparison)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                else:
                    last_dt = last_dt.astimezone(timezone.utc)
                
                now_utc = now.astimezone(timezone.utc)
                hours_since = (now_utc - last_dt).total_seconds() / 3600
                if hours_since < 20:
                    LOG.info("Fuel update already sent recently — skipping")
                    return
            except Exception as e:
                LOG.warning(f"⚠️  Recency check failed for fuel: {e}")

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
    petrol_sources = []

    if petrol_data:
        petrol_avg_formatted = petrol_data.get("avg_petrol")
        petrol_avg_raw = petrol_data.get("avg_raw")
        petrol_sources = petrol_data.get("sources", [])
        
        if not TEST_MODE and previous_petrol_raw is not None and petrol_avg_raw is not None:
            petrol_changed = abs(petrol_avg_raw - previous_petrol_raw) >= 0.01

    # Normalize LPG
    lpg_retail_range = None
    lpg_depot_per_kg_raw = None
    lpg_changed = True
    lpg_source = None

    if lpg_data and lpg_data.get("retail_estimate_lagos") != "N/A":
        lpg_retail_range = lpg_data["retail_estimate_lagos"]
        lpg_depot_per_kg_raw = lpg_data.get("depot_per_kg_raw")
        lpg_source = lpg_data.get("source")

        if not TEST_MODE and previous_lpg_raw is not None and lpg_depot_per_kg_raw is not None:
            lpg_changed = abs(lpg_depot_per_kg_raw - previous_lpg_raw) >= 0.01

    # Decide whether to post
    should_post = TEST_MODE or petrol_changed or lpg_changed or (previous_petrol_raw is None)

    if not should_post:
        LOG.info("No price change detected — skipping fuel post")
        return

    # Build message
    message_lines = [
        "🌅 <b>Weekend Energy Price Report — Nigeria</b>",
        f"📅 {now.strftime('%B %d, %Y')} • <i>Morning update</i>",
        "",
        "┌" + "─" * 38 + "┐",
    ]

    # Petrol section
    if petrol_avg_formatted:
        change_absolute = petrol_data.get("change_absolute", "N/A")
        change_percent = petrol_data.get("change_percent", "N/A")
        
        change_emoji = "•"
        if change_absolute and str(change_absolute).startswith("+"):
            change_emoji = "📈"
        elif change_absolute and str(change_absolute).startswith("-"):
            change_emoji = "📉"

        message_lines.extend([
            "│ ⛽ <b>Petrol (PMS)</b> — National Average",
            "│",
            f"│    <b>Price:</b> <code>{petrol_avg_formatted}</code>",
            f"│    {change_emoji} <b>Change:</b> {change_absolute} ({change_percent})",
            "│",
        ])
    else:
        message_lines.extend([
            "│ ⛽ <b>Petrol (PMS)</b>",
            "│    <i>Data unavailable</i>",
            "│",
        ])

    # LPG section
    if lpg_retail_range:
        lpg_depot_avg = lpg_data.get("avg_depot_20mt", "N/A")
        lpg_depot_per_kg = lpg_data.get("avg_depot_per_kg", "N/A")
        
        message_lines.extend([
            "│ 🔥 <b>LPG (Cooking Gas)</b> — Lagos Estimate",
            "│",
            f"│    <b>Depot (20MT):</b> <code>{lpg_depot_avg}</code>",
            f"│    <b>Per kg:</b> <code>{lpg_depot_per_kg}</code>",
            f"│    <b>Retail range:</b> <code>{lpg_retail_range}</code>",
            "│",
        ])
    else:
        message_lines.extend([
            "│ 🔥 <b>LPG (Cooking Gas)</b>",
            "│    <i>Data unavailable</i>",
            "│",
        ])

    message_lines.append("└" + "─" * 38 + "┘")
    message_lines.append("")

    # Sources section
    message_lines.append("<b>📍 Sources:</b>")
    
    if petrol_sources:
        for source in petrol_sources:
            # Create a clickable source with cleaner URL display
            source_display = source.replace("https://", "").replace("http://", "").rstrip("/")
            if "fuelpricewatch" in source.lower():
                message_lines.append(f"  • ⛽ <a href=\"{source}\">Fuel Price Watch</a>")
            else:
                message_lines.append(f"  • ⛽ <a href=\"{source}\">{source_display}</a>")
    
    if lpg_source:
        source_display = lpg_source.replace("https://", "").replace("http://", "").rstrip("/")
        if "lpg" in lpg_source.lower():
            message_lines.append(f"  • 🔥 <a href=\"{lpg_source}\">LPG in Nigeria</a>")
        else:
            message_lines.append(f"  • 🔥 <a href=\"{lpg_source}\">{source_display}</a>")

    message_lines.extend([
        "",
        f"🔗 <a href=\"https://t.me/real_time_alerts_energy\">Visit Real Time Alerts(ENERGY UPDATES) for more Energy updates⚡️⛽️</a>",
        "",
        f"🔗 <a href=\"https://t.me/Real_Time_Alert\">Visit Real Time Alerts(SCHOOL NEWS) for school updates 🗞️📰</a>",
        "",
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
            LOG.info(f"Fuel price update sent to {chat_id}")
        except Exception:
            LOG.exception(f"Failed to send fuel update to {chat_id}")

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
async def check_and_post_school_updates(context: ContextTypes.DEFAULT_TYPE):
    """
    Job: Scrapes school sources, extracts updates per source,
    and posts separate messages for sources with new/changed content.
    Uses dual-layer persistence with deduplication.
    """
    LOG.info("=" * 70)
    LOG.info("🏫 SCHOOL UPDATES JOB STARTED")
    LOG.info("=" * 70)

    # Debug configuration state
    force_mode = TEST_MODE or SCHOOL_FORCE_POST
    LOG.info(f"Configuration: TEST_MODE={TEST_MODE}, SCHOOL_FORCE_POST={SCHOOL_FORCE_POST}")
    LOG.info(f"Force Mode: {'🔴 ENABLED (bypassing checks)' if force_mode else '⚪ DISABLED'}")
    LOG.info(f"AUTO_POST_TO_CHANNEL={AUTO_POST_TO_CHANNEL}, CHANNEL_DEAL_CHAT_ID={CHANNEL_DEAL_CHAT_ID}")

    if not AUTO_POST_TO_CHANNEL or not CHANNEL_DEAL_CHAT_ID:
        LOG.error("❌ Channel posting disabled or CHANNEL_DEAL_CHAT_ID missing — aborting")
        return

    max_posts = MAX_CHANNEL_POSTS_PER_RUN or 5
    send_delay = 15
    eligible_candidates = []
    now = datetime.now(TIMEZONE)

    LOG.info(f"📋 DEFAULT_SCHOOL_SOURCES loaded: {len(DEFAULT_SCHOOL_SOURCES)} source(s)")

    # Convert DEFAULT_SCHOOL_SOURCES to site_configs format
    site_configs = {}
    for src_name, urls in DEFAULT_SCHOOL_SOURCES.items():
        if not urls:
            LOG.warning(f"⚠️  No URLs for {src_name}, skipping")
            continue

        base_url = urls[0]
        domain = get_domain_from_url(base_url)

        if 'myschool.ng' in domain:
            site_type = 'myschool'
        elif 'punchng.com' in domain:
            site_type = 'punch'
        elif 'nuc.edu.ng' in domain:
            site_type = 'nuc'
        else:
            site_type = 'generic'

        site_configs[src_name] = {
            'base_url': base_url,
            'type': site_type,
            'max_articles': 10
        }

        LOG.info(f"  📌 {src_name}: {base_url[:60]}... ({site_type})")

    if not site_configs:
        LOG.error("❌ No valid site configurations — aborting")
        return

    # ═══════════════════════════════════════════════════════════════════════
    # PROCESSING LOOP
    # ═══════════════════════════════════════════════════════════════════════
    processed_count = 0
    error_count = 0
    skipped_count = 0
    source_list = list(site_configs.items())
    total_sources = len(source_list)

    LOG.info(f"🔄 Beginning processing loop for {total_sources} sources...")

    idx = 0
    while idx < total_sources:
        source_name, config = source_list[idx]
        processed_count += 1

        LOG.info(f"\n{'─' * 70}")
        LOG.info(f"🔍 [{processed_count}/{total_sources}] Processing: {source_name}")
        LOG.info(f"{'─' * 70}")

        source_eligible = None
        try:
            LOG.info(f"🌐 Calling scrape_school_news for {source_name}...")

            single_site_config = {source_name: config}
            current_site_type = config.get('type', 'generic')
            
            if current_site_type == 'myschool':
                timeout_seconds = 800.0
                LOG.info(f"⏰ Using extended timeout for MySchool: {timeout_seconds}s")
            else:
                timeout_seconds = 250.0
                LOG.info(f"⏰ Using standard timeout: {timeout_seconds}s")

            items = await asyncio.wait_for(
                scrape_school_news(
                    single_site_config,
                    fetch_full_content=False,
                    max_articles=config.get('max_articles', 10)
                ),
                timeout=timeout_seconds
            )

            item_count = len(items) if items else 0
            LOG.info(f"📊 Scrape returned {item_count} items for {source_name}")

            if not items:
                LOG.info(f"⏭️  No items for {source_name}, skipping")
                skipped_count += 1
                idx += 1
                continue

            # Stable sorting
            items.sort(key=lambda x: (
                x.get('date_obj') is None,
                -x.get('date_obj', datetime.min).timestamp() if x.get('date_obj') else 0,
                x.get('title', '').lower()
            ))

            # Build message
            LOG.info(f"📝 Building message for {source_name}...")
            report_lines = [
                f"<b>📢 {source_name}</b>",
                f"<i>📅 Updated: {now.strftime('%b %d, %Y — %H:%M WAT')}</i>",
                "",
                f"<b>Found {len(items)} updates:</b>",
                "",
            ]

            for i, item in enumerate(items[:8], 1):
                try:
                    title = item.get('title', 'Untitled')
                    date = item.get('date', '')
                    snippet = item.get('snippet', '').strip()
                    link = item.get('link', '')

                    item_line = f"<b>{i}. {title}</b>"
                    if date:
                        item_line += f" — <i>{date}</i>"
                    report_lines.append(item_line)

                    if snippet and len(snippet) > 30:
                        snippet = snippet.replace("Read More", "").replace("Click to read", "").strip()
                        if len(snippet) > 150:
                            snippet = snippet[:147] + "..."
                        report_lines.append(f"   {snippet}")

                    report_lines.append(f"   🔗 <a href=\"{link}\">Read full article →</a>")
                    report_lines.append("")

                except Exception as item_err:
                    LOG.error(f"⚠️  Error processing item in {source_name}: {item_err}")
                    continue

            source_url = config.get('base_url', '')
            report_lines.extend([
                f"<i>💡 Tip: Click any link above to read the full article</i>",
                "",
                f"🔗 <a href=\"{source_url}\">Visit {source_name}</a>",
                "",
                f"🔗 <a href=\"https://t.me/real_time_alerts_energy\">Visit Real Time Alerts(ENERGY UPDATES) for Energy updates⚡️⛽️</a>",
                "",
                f"🔗 <a href=\"https://t.me/Real_Time_Alert\">Visit Real Time Alerts(SCHOOL NEWS) for more school updates 🗞️📰</a>",
                "",
                f"<i>#EducationUpdates #{source_name.replace(' ', '').replace('(', '').replace(')', '')}</i>"
            ])
            report_text = "\n".join(report_lines)

            # Truncate if needed
            original_len = len(report_text)
            if original_len > 4000:
                LOG.warning(f"⚠️  Message too long ({original_len}), truncating...")
                lines = report_text.split('\n')
                item_count = 0
                for i, line in enumerate(lines):
                    if re.match(r'^\d+\.\s+', line):
                        item_count += 1
                        if item_count >= 5:
                            lines = lines[:i]
                            lines.append("\n<i>(Additional items truncated due to message length limit)</i>")
                            lines.extend(report_lines[-4:])
                            break
                report_text = "\n".join(lines)

            LOG.info(f"📝 Message built: {len(report_text)} chars")

            # Hash computation
            try:
                hash_input_data = {
                    "title": source_name,
                    "item_count": len(items),
                    "raw": {
                        "items": [
                            {
                                "title": item.get('title'),
                                "link": item.get('link') or item.get('url', '')
                            }
                            for item in items[:5]
                        ]
                    }
                }
                
                LOG.info(f"🔐 HASH INPUT DATA for {source_name}:")
                LOG.info(f"  - Source: {source_name}")
                LOG.info(f"  - Item count: {len(items)}")
                LOG.info(f"  - Items being hashed (first 5):")
                for i, item_data in enumerate(hash_input_data["raw"]["items"], 1):
                    LOG.info(f"    {i}. Title: {item_data.get('title', 'N/A')[:80]}")
                    LOG.info(f"       Link: {item_data.get('link', 'N/A')[:80]}")
                
                content_hash = compute_content_hash(hash_input_data)
                LOG.info(f"✅ Generated hash for {source_name}: {content_hash}")
                
            except Exception as hash_err:
                LOG.error(f"❌ Hash computation failed for {source_name}: {hash_err}")
                LOG.exception(f"Hash error traceback for {source_name}:")
                content_hash = "fallback_hash_" + str(time.time())
                LOG.warning(f"⚠️  Using fallback hash: {content_hash}")

            snapshot_key = f"school_{_slugify(source_name)}"
            LOG.info(f"🔑 Snapshot key: {snapshot_key}")

            # Duplicate check
            if not force_mode:
                try:
                    LOG.info(f"🔍 Running duplicate check for {source_name}...")
                    is_duplicate = await check_duplicate_post(
                        ref=snapshot_key,
                        content_hash=content_hash,
                        lookback_hours=48
                    )
                    if is_duplicate:
                        LOG.info(f"⏭️  Duplicate detected for {source_name}, skipping")
                        skipped_count += 1
                        idx += 1
                        continue
                    else:
                        LOG.info(f"✅ Not a duplicate: {source_name}")
                except Exception as dup_err:
                    LOG.error(f"⚠️  Duplicate check failed for {source_name}: {dup_err}")
                    if not force_mode:
                        skipped_count += 1
                        idx += 1
                        continue
            else:
                LOG.info(f"🚫 Duplicate check bypassed (force mode)")

            # Recency check
            if not force_mode:
                try:
                    LOG.info(f"🕐 Running recency check for {source_name}...")
                    snapshot = await load_channel_snapshot(snapshot_key)
                    if snapshot and snapshot.get("last_posted_at"):
                        last_val = snapshot["last_posted_at"]
                        if isinstance(last_val, datetime):
                            last_dt = last_val
                        else:
                            last_val_str = last_val.replace("Z", "+00:00") if "Z" in last_val else last_val
                            last_dt = datetime.fromisoformat(last_val_str)

                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        else:
                            last_dt = last_dt.astimezone(timezone.utc)

                        now_utc = now.astimezone(timezone.utc)
                        hours_since = (now_utc - last_dt).total_seconds() / 3600
                        if hours_since < 5:
                            LOG.info(f"⏭️  Too recent ({hours_since:.1f}h < 6h), skipping")
                            skipped_count += 1
                            idx += 1
                            continue
                        else:
                            LOG.info(f"✅ Recency OK ({hours_since:.1f}h >= 6h)")
                    else:
                        LOG.info(f"✅ No previous post found (first time)")
                except Exception as rec_err:
                    LOG.error(f"⚠️  Recency check failed for {source_name}: {rec_err}")

            LOG.info(f"✅ {source_name} ELIGIBLE ({len(items)} items)")
            source_eligible = {
                "source_name": source_name,
                "report_text": report_text,
                "content_hash": content_hash,
                "snapshot_key": snapshot_key,
                "item_count": len(items),
            }

        except asyncio.TimeoutError:
            LOG.error(f"⏱️  TIMEOUT processing {source_name} after {timeout_seconds}s")
            error_count += 1
        except Exception as e:
            LOG.error(f"❌ CRITICAL ERROR in {source_name}: {str(e)}")
            LOG.exception(f"Full traceback for {source_name}:")
            error_count += 1

        if source_eligible:
            eligible_candidates.append(source_eligible)
            LOG.info(f"🏁 {source_name}: ✅ ELIGIBLE ADDED")
        else:
            LOG.info(f"🏁 {source_name}: ❌ NOT ELIGIBLE")

        idx += 1
        await asyncio.sleep(5)

    # ═══════════════════════════════════════════════════════════════════════
    # END OF LOOP
    # ═══════════════════════════════════════════════════════════════════════
    LOG.info(f"\n{'=' * 70}")
    LOG.info(f"📊 LOOP COMPLETE")
    LOG.info(f"{'=' * 70}")
    LOG.info(f"Processed: {processed_count}/{total_sources}")
    LOG.info(f"Skipped:   {skipped_count}")
    LOG.info(f"Errors:    {error_count}")
    LOG.info(f"Eligible:  {len(eligible_candidates)}")

    if not eligible_candidates:
        LOG.warning("⚠️  No eligible candidates - nothing to post")
        return

    LOG.info(f"Eligible sources: {[e['source_name'] for e in eligible_candidates]}")

    # ═══════════════════════════════════════════════════════════════════════
    # POSTING PHASE
    # ═══════════════════════════════════════════════════════════════════════
    LOG.info(f"\n{'=' * 70}")
    LOG.info(f"📤 ENTERING POSTING PHASE")
    LOG.info(f"{'=' * 70}")

    posted_count = 0
    targets = CHANNEL_DEAL_CHAT_ID if isinstance(CHANNEL_DEAL_CHAT_ID, list) else [CHANNEL_DEAL_CHAT_ID]

    if not targets:
        LOG.error("❌ No targets configured!")
        return

    LOG.info(f"📡 Targets: {targets}")

    try:
        eligible_candidates.sort(key=lambda x: -x.get("item_count", 0))
        to_post = eligible_candidates[:max_posts]
        LOG.info(f"📋 Will post {len(to_post)}/{len(eligible_candidates)} sources")
    except Exception as sort_err:
        LOG.error(f"❌ Sorting failed: {sort_err}, using unsorted")
        to_post = eligible_candidates[:max_posts]

    # Post each one
    for post_idx, item in enumerate(to_post, 1):
        source_name = item.get('source_name', 'Unknown')
        LOG.info(f"\n{'─' * 70}")
        LOG.info(f"📨 [{post_idx}/{len(to_post)}] Posting: {source_name}")
        LOG.info(f"{'─' * 70}")

        message = item.get('report_text', '')
        if not message:
            LOG.error(f"❌ No report_text for {source_name}, skipping")
            continue

        if len(message) > 4096:
            LOG.warning(f"⚠️  Message too long ({len(message)}), truncating...")
            message = message[:4090] + "..."

        LOG.info(f"📝 Final message length: {len(message)}")

        sent_to_any = False
        for chat_id in targets:
            LOG.info(f"📤 Sending to {chat_id}...")
            try:
                if not isinstance(chat_id, (int, str)):
                    LOG.error(f"❌ Invalid chat_id type: {type(chat_id)}")
                    continue

                response = await asyncio.wait_for(
                    context.bot.send_message(
                        chat_id=chat_id,
                        text=message,
                        parse_mode="HTML",
                        disable_web_page_preview=False,
                    ),
                    timeout=30.0
                )
                sent_to_any = True
                LOG.info(f"✅ SENT to {chat_id} (msg_id: {response.message_id})")

            except asyncio.TimeoutError:
                LOG.error(f"⏱️  TIMEOUT sending to {chat_id}")
            except Exception as send_err:
                LOG.error(f"❌ FAILED to send to {chat_id}: {str(send_err)}")

        # ═══════════════════════════════════════════════════════════════════
        # POST-POSTING: SAVE & VERIFY
        # ═══════════════════════════════════════════════════════════════════
        if sent_to_any:
            posted_count += 1
            LOG.info(f"✅ POST SUCCESS: {source_name}")
            LOG.info(f"")
            LOG.info(f"{'🔸' * 35}")
            LOG.info(f"💾 BEGINNING SAVE OPERATIONS for {source_name}")
            LOG.info(f"{'🔸' * 35}")

            if not force_mode:
                try:
                    snapshot_key = item.get("snapshot_key")
                    content_hash = item.get("content_hash")
                    
                    LOG.info(f"📋 Preparing snapshot data...")
                    snapshot_data = {
                        "content_hash": content_hash,
                        "item_count": item.get("item_count"),
                        "site": source_name,
                        "title": f"School Updates - {source_name}",
                    }
                    LOG.info(f"   ├─ ref: {snapshot_key}")
                    LOG.info(f"   ├─ content_hash: {content_hash}")
                    LOG.info(f"   ├─ item_count: {snapshot_data['item_count']}")
                    LOG.info(f"   ├─ site: {snapshot_data['site']}")
                    LOG.info(f"   └─ title: {snapshot_data['title']}")
                    
                    # Step 1: Save snapshot
                    LOG.info(f"")
                    LOG.info(f"📥 STEP 1: Calling mark_as_posted()...")
                    await mark_as_posted(snapshot_key, snapshot_data)
                    LOG.info(f"   ✅ mark_as_posted() completed")
                    
                    # Step 2: Record dedup hash
                    LOG.info(f"")
                    LOG.info(f"🔒 STEP 2: Calling record_posted_hash()...")
                    await record_posted_hash(
                        ref=snapshot_key,
                        content_hash=content_hash,
                        snapshot=snapshot_data
                    )
                    LOG.info(f"   ✅ record_posted_hash() completed")
                    
                    # Step 3: Verify what was saved
                    LOG.info(f"")
                    LOG.info(f"🔍 STEP 3: VERIFICATION - Checking what was saved...")
                    LOG.info(f"")
                    
                    # 3a. Check snapshot
                    LOG.info(f"   📂 Checking snapshot in storage...")
                    saved_snapshot = await load_channel_snapshot(snapshot_key)
                    if saved_snapshot:
                        LOG.info(f"   ✅ Snapshot FOUND in storage:")
                        LOG.info(f"      ├─ ref: {saved_snapshot.get('ref', 'N/A')}")
                        LOG.info(f"      ├─ content_hash: {saved_snapshot.get('content_hash', 'N/A')}")
                        LOG.info(f"      ├─ last_posted_at: {saved_snapshot.get('last_posted_at', 'N/A')}")
                        LOG.info(f"      ├─ item_count: {saved_snapshot.get('item_count', 'N/A')}")
                        LOG.info(f"      └─ site: {saved_snapshot.get('site', 'N/A')}")
                    else:
                        LOG.error(f"   ❌ Snapshot NOT FOUND in storage!")
                    
                    # 3b. Check duplicate detection
                    LOG.info(f"")
                    LOG.info(f"   🔍 Running duplicate check to verify hash was saved...")
                    is_now_duplicate = await check_duplicate_post(
                        ref=snapshot_key,
                        content_hash=content_hash,
                        lookback_hours=48
                    )
                    if is_now_duplicate:
                        LOG.info(f"   ✅ VERIFICATION PASSED: Hash found in dedup storage")
                        LOG.info(f"      → Future posts with this hash will be skipped ✅")
                    else:
                        LOG.error(f"   ❌ VERIFICATION FAILED: Hash NOT found in dedup storage!")
                        LOG.error(f"      → Future duplicate posts WILL NOT be prevented! ⚠️")
                    
                    LOG.info(f"")
                    LOG.info(f"{'🔸' * 35}")
                    LOG.info(f"💾 SAVE OPERATIONS COMPLETE for {source_name}")
                    LOG.info(f"{'🔸' * 35}")
                    LOG.info(f"")
                    
                except Exception as snap_err:
                    LOG.error(f"❌ POST RECORDING FAILED: {snap_err}")
                    LOG.exception(f"Full traceback:")
            else:
                LOG.info(f"🚫 Save operations skipped (force mode enabled)")

            # Delay before next post
            if post_idx < len(to_post):
                LOG.info(f"⏳ Waiting {send_delay}s before next post...")
                await asyncio.sleep(send_delay)
        else:
            LOG.error(f"❌ COMPLETE FAILURE: {source_name} not sent to any target")

    # ═══════════════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═══════════════════════════════════════════════════════════════════════
    LOG.info(f"\n{'=' * 70}")
    LOG.info(f"🏁 FINAL RESULT: {posted_count}/{len(to_post)} sources posted")
    LOG.info(f"{'=' * 70}\n")

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

async def run_bot():
    """
    Main bot runner with PTB v20+/v21 compatibility.
    Enhanced with granular error handling, fail-fast validation, and safe teardown.
    """
    global application
    application = None

    # ─────────────────────────────────────────────────────────────────────
    # VALIDATION PHASE (Fail-Fast)
    # ─────────────────────────────────────────────────────────────────────
    
    LOG.info("\n" + "=" * 70)
    LOG.info("🔍 BOT VALIDATION PHASE")
    LOG.info("=" * 70)
    
    required_vars = ["TELEGRAM_TOKEN", "REDIS_URL", "DB_URL"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        error_msg = f"❌ Missing required environment variables: {', '.join(missing_vars)}"
        LOG.error(error_msg)
        raise ValueError(error_msg)
        
    TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
    REDIS_URL = os.getenv("REDIS_URL")
    
    LOG.info("   ✓ Environment variables configured successfully")
    LOG.info("=" * 70)

    r = None
    lock = None
    renewal_task = None

    try:
        # ─────────────────────────────────────────────────────────────────────
        # DATABASE INITIALIZATION
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n📦 Initializing database...")
        try:
            initialize_database()
            LOG.info("   ✓ Database tables ready")
        except Exception as e:
            LOG.exception("   ✗ Database initialization failed.")
            
        
        # ─────────────────────────────────────────────────────────────────────
        # REDIS CONNECTION
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n🔴 Connecting to Redis...")
        try:
            r = await asyncio.wait_for(
                redis_async.from_url(REDIS_URL, decode_responses=True),
                timeout=10.0
            )
            await asyncio.wait_for(r.ping(), timeout=5.0)
            LOG.info("   ✓ Redis connected and ping successful")
            
        except asyncio.TimeoutError:
            LOG.error("   ❌ Redis connection timeout (10s)")
            raise RuntimeError("Redis connection timeout")
        except RedisConnectionError as e:
            LOG.error(f"   ❌ Redis connection refused or invalid URL: {e}")
            raise RuntimeError(f"Redis connection failed: {e}") from e
        except Exception as e:
            LOG.exception("   ✗ Unexpected Redis error.")
            
        
        # ─────────────────────────────────────────────────────────────────────
        # DISTRIBUTED LOCK
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n🔐 Acquiring distributed lock...")
        try:
            lock_acquired, lock_info = await asyncio.wait_for(
                acquire_long_running_lock(r),
                timeout=60.0
            )
            
            if not lock_acquired:
                LOG.warning("   ⚠️  Could not acquire lock — another instance is running")
                
            
            lock, renewal_task = lock_info
            LOG.info("   ✓ Lock acquired and renewal task started")
            
        except asyncio.TimeoutError:
            LOG.error("   ❌ Lock acquisition timeout (60s)")
            raise RuntimeError("Lock acquisition timeout")
        except RedisError as e:
            LOG.error(f"   ❌ Redis error during lock acquisition: {e}")
            
        
        # ─────────────────────────────────────────────────────────────────────
        # TELEGRAM APPLICATION BUILD
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n📱 Building Telegram Application...")
        try:
            application = Application.builder().token(TELEGRAM_TOKEN).build()
            LOG.info("   ✓ Application builder created")
            
            await asyncio.wait_for(application.initialize(), timeout=15.0)
            LOG.info("   ✓ Application initialized")
            
        except InvalidToken:
            LOG.error("   ❌ TELEGRAM_TOKEN is invalid or rejected by Telegram API.")
            raise RuntimeError("Invalid Telegram Token")
        except asyncio.TimeoutError:
            LOG.error("   ❌ Application initialization timeout (15s)")
            raise RuntimeError("Application initialization timeout")
        except TelegramError as e:
            LOG.exception("   ✗ Telegram API error during initialization.")
            raise RuntimeError(f"Telegram initialization failed: {e}") from e
        
        # ─────────────────────────────────────────────────────────────────────
        # HANDLERS & ERROR HANDLING
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n🎛️  Adding handlers and error handler...")
        try:
            handlers = get_application_handlers()
            LOG.info(f"   → Adding {len(handlers)} command handlers")
            for handler in handlers:
                application.add_handler(handler)
            LOG.info("   ✓ Handlers added")
            
            application.add_error_handler(global_error_handler)
            LOG.info("   ✓ Error handler registered")
        except Exception as e:
            LOG.exception("   ✗ Handler setup failed.")
            raise RuntimeError(f"Handler setup failed: {e}") from e
        
        # ─────────────────────────────────────────────────────────────────────
        # WEBHOOK CLEANUP
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n🌐 Clearing any existing webhooks...")
        try:
            if getattr(application, 'bot', None):
                await asyncio.wait_for(
                    application.bot.delete_webhook(drop_pending_updates=True),
                    timeout=10.0
                )
                LOG.info("   ✓ Webhook cleared")
        except asyncio.TimeoutError:
            LOG.warning("   ⚠️  Webhook cleanup timed out, proceeding anyway.")
        except Exception as e:
            LOG.debug(f"   ℹ️  Webhook cleanup (non-critical): {e}")
        
        # ─────────────────────────────────────────────────────────────────────
        # START APPLICATION & POLLING
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("\n🚀 Starting Telegram application...")
        try:
            await asyncio.wait_for(application.start(), timeout=10.0)
            LOG.info("   ✓ Application started")
            
            LOG.info("   → Starting long-polling...")
            await asyncio.wait_for(
                application.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES
                ),
                timeout=10.0
            )
            LOG.info("   ✓ Polling started")
            
        except Conflict:
            LOG.error("   ❌ Polling conflict: Another bot instance is currently polling.")
            raise RuntimeError("Telegram polling conflict")
        except NetworkError as e:
            LOG.error(f"   ❌ Network error starting application: {e}")
            raise RuntimeError(f"Network error starting bot: {e}") from e
        except asyncio.TimeoutError:
            LOG.error("   ❌ Application startup/polling timeout (10s)")
            raise RuntimeError("Application startup timeout")
        
        LOG.info("\n" + "=" * 70)
        LOG.info("✅ BOT IS RUNNING - Listening for updates")
        LOG.info("=" * 70 + "\n")
        
        # ─────────────────────────────────────────────────────────────────────
        # KEEP RUNNING FOREVER
        # ─────────────────────────────────────────────────────────────────────
        LOG.info("⏳ Entering infinite wait loop (bot will keep running)...")
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        LOG.info("⚠️  Bot task cancelled — initiating graceful shutdown")

    except Exception as exc:
        LOG.error("\n" + "=" * 70)
        LOG.error("💥 FATAL ERROR IN BOT")
        LOG.error("=" * 70)
        LOG.exception("Exception Details: %s", exc)
        LOG.error("=" * 70)
        raise  # Re-raise so run_bot_with_signal can catch it

    finally:
        LOG.info("\n🛑 CLEANING UP BOT RESOURCES...")
        
        # Stop application (Using getattr to prevent AttributeError if crash happened before initialization)
        if application:
            if getattr(application, 'updater', None) and application.updater.running:
                LOG.info("   → Stopping updater...")
                try:
                    await asyncio.wait_for(application.updater.stop(), timeout=5.0)
                    LOG.info("   ✓ Updater stopped")
                except Exception as e:
                    LOG.warning(f"   ⚠️  Updater stop error: {e}")
            
            if application.running:
                LOG.info("   → Stopping application...")
                try:
                    await asyncio.wait_for(application.stop(), timeout=5.0)
                    LOG.info("   ✓ Application stopped")
                except Exception as e:
                    LOG.warning(f"   ⚠️  Application stop error: {e}")
            
            LOG.info("   → Shutting down application...")
            try:
                await asyncio.wait_for(application.shutdown(), timeout=5.0)
                LOG.info("   ✓ Application shutdown complete")
            except Exception as e:
                LOG.warning(f"   ⚠️  Application shutdown error: {e}")

        # Cancel renewal task
        if renewal_task and not renewal_task.done():
            LOG.info("   → Cancelling lock renewal...")
            renewal_task.cancel()
            try:
                await asyncio.wait_for(renewal_task, timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                LOG.info("   ✓ Lock renewal cancelled")
            except Exception as e:
                LOG.warning(f"   ⚠️  Lock renewal cleanup issue: {e}")

        # Release lock safely
        if lock and r:
            LOG.info("   → Releasing Redis lock...")
            try:
                if await asyncio.wait_for(lock.owned(), timeout=2.0):
                    await asyncio.wait_for(lock.release(), timeout=2.0)
                    LOG.info("   ✓ Redis lock released")
            except Exception as e:
                LOG.warning(f"   ⚠️  Lock release error: {e}")

        # Close Redis
        if r:
            LOG.info("   → Closing Redis connection...")
            try:
                await asyncio.wait_for(r.aclose(), timeout=2.0)
                LOG.info("   ✓ Redis connection closed")
            except Exception as e:
                LOG.warning(f"   ⚠️  Redis close error: {e}")

        LOG.info("\n" + "=" * 70)
        LOG.info("🏁 Bot cleanup complete")
        LOG.info("=" * 70 + "\n")