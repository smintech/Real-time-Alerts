# commands.py (updated)
import asyncio
import logging
import time
import urllib.parse
from typing import List, Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters

from bot.settings import (
    MAX_WATCHES_FREE,
    ADD_COOLDOWN_SECONDS,
    SCRAPE_TIMEOUT,
    ALLOWED_DIRECTIONS,
    CATEGORIES,
    PAID_TIERS,
    MIN_CHANGE_TO_ALERT,
)
from utils.config import ADMIN_IDS
from utils.utils import scrape_product

# -------------------------
# In-memory stores (MVP – replace with DB soon)
# -------------------------
user_watches: Dict[int, List[Dict]] = {}  # {user_id: [{"url": ..., "category": ...}]}
_user_last_action: Dict[int, float] = {}  # cooldowns
user_subscriptions: Dict[int, Dict] = {}  # {user_id: {"type": "personal", "tier": "free", "trial_start": time?, "paid": False}}
user_settings: Dict[int, Dict] = {}  # {user_id: {"enabled_categories": [...]}}

# -------------------------
# Configuration
# -------------------------
LOG = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DEFAULT_FREE_LIMIT = MAX_WATCHES_FREE


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


def get_user_max_watches(user_id: int) -> int:
    sub = user_subscriptions.get(user_id, {"tier": "free"})
    tier = sub["tier"]
    
    if tier == "free":
        return DEFAULT_FREE_LIMIT
    
    if tier in PAID_TIERS:
        if "trial_start" in sub:
            days_since = (time.time() - sub["trial_start"]) / 86400
            if days_since <= PAID_TIERS[tier]["trial_days"]:
                return PAID_TIERS[tier]["max_watches"]
        
        if sub.get("paid", False):
            return PAID_TIERS[tier]["max_watches"]
    
    return DEFAULT_FREE_LIMIT


def build_main_menu(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("➕ Add New Watch", callback_data="add_watch_inline")],
        [InlineKeyboardButton("📋 My Watches", callback_data="my_watches_inline")],
        [InlineKeyboardButton("🔥 Hot Deals Channel", callback_data="hot_channel")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard_inline")],
        [InlineKeyboardButton("🔔 Alert Categories", callback_data="category_settings")],
        [InlineKeyboardButton("❓ Help", callback_data="help_inline")],
    ]
    # Admin button visible only to admins
    if user_id is not None and user_id in ADMIN_IDS:
        keyboard.insert(0, [InlineKeyboardButton("🛠️ Admin Panel", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)


def build_simple_back(target="main_menu_inline", label="↩️ Back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])


def build_category_selection() -> InlineKeyboardMarkup:
    keyboard = []
    emoji_map = {
        'phones': '📱',
        'gadgets': '🎧',
        'laptops': '💻',
        'accessories': '🔌'
    }
    for cat in CATEGORIES:
        emoji = emoji_map.get(cat, '📦')
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat.capitalize()}", callback_data=f"cat_select_{cat}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("↩️ Cancel", callback_data="add_watch_cancel")
    ])
    
    return InlineKeyboardMarkup(keyboard)


def build_category_settings_menu(user_id: int) -> InlineKeyboardMarkup:
    current_enabled = user_settings.get(user_id, {}).get("enabled_categories", CATEGORIES.copy())
    
    keyboard = []
    for cat in CATEGORIES:
        prefix = "✅ " if cat in current_enabled else "⬜ "
        keyboard.append([
            InlineKeyboardButton(f"{prefix}{cat.capitalize()}", callback_data=f"toggle_cat_{cat}")
        ])
    
    keyboard.append([InlineKeyboardButton("💾 Save & Exit", callback_data="save_categories")])
    keyboard.append([InlineKeyboardButton("↩️ Back to Main", callback_data="main_menu_inline")])
    
    return InlineKeyboardMarkup(keyboard)


# -------------------------
# Admin helpers: stats & user list pagination (10 per page)
# -------------------------
def _build_admin_panel() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton("📊 User Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="admin_broadcast")],
        [InlineKeyboardButton("↩️ Back", callback_data="main_menu_inline")],
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_users_page(page: int = 0, per_page: int = 10) -> InlineKeyboardMarkup:
    """
    Build inline keyboard of user IDs (paginated).
    """
    all_user_ids = list(user_watches.keys())
    total = len(all_user_ids)
    start = page * per_page
    end = start + per_page
    page_items = all_user_ids[start:end]

    keyboard = []
    for uid in page_items:
        # show small stats per user if available
        watches = user_watches.get(uid, [])
        label = f"{uid} ({len(watches)} watches)"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_user_{uid}")])

    nav_row = []
    if start > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_page_{page-1}"))
    if end < total:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_page_{page+1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("↩️ Back to Admin", callback_data="admin_panel")])
    return InlineKeyboardMarkup(keyboard)


def _compute_user_stats() -> Dict[str, Any]:
    """
    Return simple aggregated stats for admin dashboard.
    """
    total_users = len(user_watches)
    total_watches = sum(len(w) for w in user_watches.values())
    active_watches = sum(1 for watches in user_watches.values() for w in watches if w.get("status") == "active")
    tiers = {}
    for uid, sub in user_subscriptions.items():
        tier = sub.get("tier", "free")
        tiers[tier] = tiers.get(tier, 0) + 1

    return {
        "total_users": total_users,
        "total_watches": total_watches,
        "active_watches": active_watches,
        "by_tier": tiers
    }


# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id in user_subscriptions:
        # Existing user → show main menu
        await update.message.reply_text(
            "👋 Welcome back to Naija Price Alerts!\n\nWhat would you like to do?",
            reply_markup=build_main_menu(user_id)
        )
        return

    # New user → onboarding
    keyboard = [
        [InlineKeyboardButton("🛍️ Personal (myself/family)", callback_data="onboard_personal")],
        [InlineKeyboardButton("🏪 Merchant/Reseller", callback_data="onboard_merchant")],
        [InlineKeyboardButton("🏢 Business/Agency", callback_data="onboard_business")],
    ]

    await update.message.reply_text(
        "👋 **Welcome to Naija Price Alerts!**\n\n"
        "To get started, please tell us who you are (this helps us give you the right limits and features):\n\n"
        "• **Personal** – track a few items for shopping\n"
        "• **Merchant** – monitor competitors & market prices\n"
        "• **Business** – bulk tracking & advanced use\n\n"
        "Choose your type below ↓",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def add_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    # Ensure settings exist
    if user_id not in user_settings:
        user_settings[user_id] = {
            "enabled_categories": CATEGORIES.copy(),
            "min_change_percent": MIN_CHANGE_TO_ALERT,
        }
    
    max_allowed = get_user_max_watches(user_id)
    watches = user_watches.get(user_id, [])
    if len(watches) >= max_allowed:
        tier_name = user_subscriptions.get(user_id, {}).get("tier", "free").capitalize()
        msg = (
            f"🚫 Limit reached ({max_allowed} watches) for {tier_name} tier.\n\n"
            "Upgrade for more!"
        )
        keyboard = [[InlineKeyboardButton("See Upgrades", callback_data="upgrade_plans")]]
        await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard))
        return
    
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
        await update.message.reply_text(f"⏳ Wait {int(cooldown_left)}s.")
        return

    if direction in ["high", "both"]:
        await update.message.reply_text(
            "ℹ️ Increase/any-change alerts premium. Defaulting to drops."
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
            raise ValueError("Out of stock or delisted")

        watch = {
            "url": url,
            "title": title,
            "last_price": current_price,
            "target_price": target_price,
            "direction": direction,
            "added_at": int(time.time()),
            "status": "active"
            # category added via inline flow
        }
        watches.append(watch)
        user_watches[user_id] = watches

        msg = f"✅ Added: {title}\nCurrent: {safe_price_format(current_price)}"
        if target_price:
            msg += f"\nTarget: {safe_price_format(target_price)}"
        msg += f"\nMode: {direction}"
        await update.message.reply_text(msg)

    except asyncio.TimeoutError:
        await update.message.reply_text("⚠️ Timeout - try again.")
    except ValueError as ve:
        await update.message.reply_text(f"❌ {str(ve)}")
    except Exception as exc:
        LOG.exception("Add failed: %s", exc)
        await update.message.reply_text("❌ Validation failed - check URL.")


async def list_watches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in user_settings:
        user_settings[user_id] = {"enabled_categories": CATEGORIES.copy()}
    
    enabled = user_settings[user_id]["enabled_categories"]
    text = "*Your Watches*\n"
    if enabled != CATEGORIES:
        text += f"\nEnabled categories: {', '.join(enabled)}"
    else:
        text += "\nAll categories enabled"
    
    watches = user_watches.get(user_id, [])
    if not watches:
        await update.message.reply_text(f"{text}\n\n📭 No watches yet.")
        return

    lines = [text]
    for i, w in enumerate(watches, 1):
        cat = w.get("category", "--").capitalize()
        title = w.get("title", "Unknown")
        url = w.get("url", "")
        price = safe_price_format(w.get("last_price"))
        target = f" Target ≤ {safe_price_format(w.get('target_price'))}" if w.get("target_price") else ""
        mode = {"low": "drops", "high": "increases", "both": "any"}.get(w.get("direction", "low"), "drops")
        lines.append(f"{i}. {title} ({cat})\n   Price: {price}{target} | Mode: {mode}\n   {url}")

    message = "\n\n".join(lines)
    for chunk in [message[i:i+3500] for i in range(0, len(message), 3500)]:
        await update.message.reply_text(chunk, parse_mode="Markdown")


async def remove_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Usage: /remove <number>")
        return

    try:
        idx = int(context.args[0]) - 1
    except ValueError:
        await update.message.reply_text("❌ Valid number required.")
        return

    watches = user_watches.get(user_id, [])
    if not 0 <= idx < len(watches):
        await update.message.reply_text("❌ Invalid number.")
        return

    removed = watches.pop(idx)
    await update.message.reply_text(f"🗑️ Removed: {removed.get('title', 'Watch')}")


async def assign_trial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("❌ Admin only.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /assign_trial <user_id> <tier>")
        return

    try:
        target_id = int(context.args[0])
        tier = context.args[1].lower()
        if tier not in PAID_TIERS:
            await update.message.reply_text(f"Invalid tier. Available: {', '.join(PAID_TIERS)}")
            return
        
        user_subscriptions[target_id] = {
            "tier": tier,
            "trial_start": time.time(),
            "paid": False
        }
        
        await update.message.reply_text(f"✅ Assigned {tier} trial to user {target_id}")
        
        # Notify user
        msg = f"🎉 You've been granted a {PAID_TIERS[tier]['trial_days']}-day free trial for {PAID_TIERS[tier]['name']}!"
        await context.bot.send_message(chat_id=target_id, text=msg)
        
    except ValueError:
        await update.message.reply_text("❌ Invalid user ID.")


# -------------------------
# Inline callback handler (extended for admin flows)
# -------------------------
async def inline_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    try:
        # --- Onboarding and core flows (unchanged) ---
        if data.startswith("onboard_"):
            user_type = data.replace("onboard_", "")
            tier = "free"
            trial_days = 0
            
            if user_type == "merchant":
                tier = "merchant"
                trial_days = PAID_TIERS[tier]["trial_days"]
            elif user_type == "business":
                tier = "business"
                trial_days = PAID_TIERS[tier]["trial_days"]
            
            user_subscriptions[user_id] = {
                "type": user_type,
                "tier": tier,
                "trial_start": time.time() if trial_days > 0 else None,
                "paid": False
            }
            
            msg = f"✅ Set as {user_type.capitalize()} user."
            if trial_days > 0:
                msg += f" Starting {trial_days}-day free trial!"
            
            await query.edit_message_text(msg + "\n\nWhat next?", reply_markup=build_main_menu(user_id))
            return

        if data == "main_menu_inline":
            await query.edit_message_text(
                "🏠 Main Menu\n\nWhat would you like to do?",
                reply_markup=build_main_menu(user_id)
            )
            return

        elif data == "add_watch_inline":
            await query.edit_message_text(
                "Choose category:",
                reply_markup=build_category_selection()
            )
            return

        elif data.startswith("cat_select_"):
            cat = data.replace("cat_select_", "")
            if cat not in CATEGORIES:
                return
            
            context.user_data["add_category"] = cat
            await query.edit_message_text(
                f"Category: {cat.capitalize()}\n\nSend the Jumia URL:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Cancel", callback_data="add_watch_cancel")
                ]])
            )
            context.user_data["awaiting_add_url"] = True
            return

        elif data == "add_watch_cancel":
            context.user_data.clear()
            await query.edit_message_text("Cancelled.", reply_markup=build_main_menu(user_id))
            return

        elif data == "category_settings":
            await query.edit_message_text(
                "🔔 Select alert categories:",
                reply_markup=build_category_settings_menu(user_id)
            )
            return

        elif data.startswith("toggle_cat_"):
            cat = data.replace("toggle_cat_", "")
            if cat not in CATEGORIES:
                return
            
            settings = user_settings.setdefault(user_id, {"enabled_categories": CATEGORIES.copy()})
            enabled = settings["enabled_categories"]
            
            if cat in enabled:
                enabled.remove(cat)
            else:
                enabled.append(cat)
            
            await query.edit_message_reply_markup(
                reply_markup=build_category_settings_menu(user_id)
            )
            return

        elif data == "save_categories":
            await query.edit_message_text(
                "✅ Categories saved!",
                reply_markup=build_main_menu(user_id)
            )
            return

        elif data == "upgrade_plans":
            lines = ["💎 Upgrade Options\n"]
            for key, info in PAID_TIERS.items():
                trial = f"{info['trial_days']}-day trial" if info['trial_days'] > 0 else ""
                lines.append(
                    f"**{info['name']}** - ₦{info['price_monthly_ngn']:,}/mo\n"
                    f"• Watches: {info['max_watches']}\n"
                    f"• {trial}\n"
                    f"• {', '.join(info['features'])}\n"
                )
            lines.append("Contact support to upgrade!")
            
            await query.edit_message_text("\n".join(lines), reply_markup=build_simple_back())
            return

        elif data == "my_watches_inline":
            # Call list, but since it's callback, simulate update
            temp_update = Update(update.update_id, message=query.message)
            await list_watches(temp_update, context)
            return

        elif data == "hot_channel":
            await query.edit_message_text(
                "🔥 Join Hot Deals: https://t.me/YourChannelNameHere",
                reply_markup=build_simple_back()
            )
            return

        elif data == "dashboard_inline":
            count = len(user_watches.get(user_id, []))
            await query.edit_message_text(
                f"📊 Dashboard\n\nWatches: {count}",
                reply_markup=build_simple_back()
            )
            return

        elif data == "help_inline":
            await query.edit_message_text(
                "❓ Help\n\n/add <url>\n/list\n/remove <num>\n\nMessage for questions!",
                reply_markup=build_simple_back()
            )
            return

        # ----------------- ADMIN flows (inline only) -----------------
        if data == "admin_panel":
            # Only admins allowed
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("⛔ Admin only.", reply_markup=build_main_menu(user_id))
                return
            await query.edit_message_text("🛠️ Admin Panel", reply_markup=_build_admin_panel())
            return

        if data == "admin_stats":
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("⛔ Admin only.", reply_markup=build_main_menu(user_id))
                return
            stats = _compute_user_stats()
            lines = [
                "📊 User Stats",
                f"• Total users: {stats['total_users']}",
                f"• Total watches: {stats['total_watches']}",
                f"• Active watches: {stats['active_watches']}",
                "• By tier:"
            ]
            for tier, count in stats["by_tier"].items():
                lines.append(f"   - {tier}: {count}")
            await query.edit_message_text("\n".join(lines), reply_markup=_build_admin_panel())
            return

        if data == "admin_broadcast":
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("⛔ Admin only.", reply_markup=build_main_menu(user_id))
                return
            keyboard = [
                [InlineKeyboardButton("📣 Broadcast to all users", callback_data="admin_broadcast_all")],
                [InlineKeyboardButton("👤 Broadcast to single user", callback_data="admin_broadcast_single")],
                [InlineKeyboardButton("↩️ Back", callback_data="admin_panel")]
            ]
            await query.edit_message_text("Broadcast options:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

        if data == "admin_broadcast_all":
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("⛔ Admin only.", reply_markup=build_main_menu(user_id))
                return
            # set state and prompt admin to send the message as text
            context.user_data["awaiting_broadcast"] = True
            context.user_data["broadcast_target"] = "all"
            await query.edit_message_text(
                "📣 Send the message you want to broadcast to *all users*.\n\n"
                "Type the message now. To cancel, send 'CANCEL'.",
                parse_mode="Markdown",
                reply_markup=build_simple_back("admin_panel", "↩️ Cancel")
            )
            return

        if data == "admin_broadcast_single":
            if user_id not in ADMIN_IDS:
                await query.edit_message_text("⛔ Admin only.", reply_markup=build_main_menu(user_id))
                return
            # show first page of users
            await query.edit_message_text("Select a user to message:", reply_markup=_build_users_page(0))
            return

        # pagination for users list
        if data.startswith("admin_users_page_"):
            if user_id not in ADMIN_IDS:
                return
            try:
                page = int(data.replace("admin_users_page_", ""))
            except Exception:
                page = 0
            await query.edit_message_text("Select a user to message:", reply_markup=_build_users_page(page))
            return

        # single user selected
        if data.startswith("admin_user_"):
            if user_id not in ADMIN_IDS:
                return
            try:
                target_user_id = int(data.replace("admin_user_", ""))
            except Exception:
                await query.edit_message_text("Invalid user selection.", reply_markup=_build_users_page(0))
                return
            # ask admin to send the message body; record target in user_data
            context.user_data["awaiting_broadcast"] = True
            context.user_data["broadcast_target"] = target_user_id
            await query.edit_message_text(
                f"📩 Send the message you want to deliver to user `{target_user_id}`.\n\n"
                "Type the message now. To cancel, send 'CANCEL'.",
                parse_mode="Markdown",
                reply_markup=build_simple_back("admin_panel", "↩️ Cancel")
            )
            return

        # fallback for other inline handlers already implemented above
    except Exception as exc:
        LOG.exception("Inline error")
        try:
            await query.edit_message_text("⚠️ Error - try /start")
        except Exception:
            pass


# This handler already exists in your original file; extended to handle awaiting_broadcast in context.user_data
async def process_add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # First: handle broadcast flow if admin initiated it
    if context.user_data.get("awaiting_broadcast"):
        admin_id = update.effective_user.id
        if admin_id not in ADMIN_IDS:
            await update.message.reply_text("⛔ Admin only.")
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("broadcast_target", None)
            return

        text = update.message.text.strip()
        if not text or text.upper() == "CANCEL":
            await update.message.reply_text("Broadcast cancelled.", reply_markup=build_main_menu(admin_id))
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("broadcast_target", None)
            return

        target = context.user_data.get("broadcast_target")
        sent = 0
        failed = 0
        failed_ids = []

        # Broadcast to all users
        if target == "all":
            # send messages sequentially with small delay to avoid rate limits
            for uid in list(user_watches.keys()):
                try:
                    await context.bot.send_message(chat_id=uid, text=text)
                    sent += 1
                    # small throttle
                    await asyncio.sleep(0.05)
                except Exception:
                    failed += 1
                    failed_ids.append(uid)
                    # continue to next user
            summary = f"Broadcast complete.\nSent: {sent}\nFailed: {failed}"
            if failed_ids:
                summary += f"\nFailed IDs: {failed_ids[:10]}{'...' if len(failed_ids) > 10 else ''}"
            await update.message.reply_text(summary, reply_markup=build_main_menu(admin_id))
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("broadcast_target", None)
            return

        # Broadcast to single user id
        try:
            target_uid = int(target)
        except Exception:
            await update.message.reply_text("Invalid target for broadcast.", reply_markup=build_main_menu(admin_id))
            context.user_data.pop("awaiting_broadcast", None)
            context.user_data.pop("broadcast_target", None)
            return

        try:
            await context.bot.send_message(chat_id=target_uid, text=text)
            await update.message.reply_text(f"Message sent to {target_uid}.", reply_markup=build_main_menu(admin_id))
        except Exception as e:
            LOG.exception("Failed sending broadcast to %s", target_uid)
            await update.message.reply_text(f"Failed to send to {target_uid}: {e}", reply_markup=build_main_menu(admin_id))

        context.user_data.pop("awaiting_broadcast", None)
        context.user_data.pop("broadcast_target", None)
        return

    # Otherwise preserve original add URL flow (as in your file)
    if not context.user_data.get("awaiting_add_url"):
        return

    url = update.message.text.strip()
    user_id = update.effective_user.id

    if not is_valid_jumia_url(url):
        await update.message.reply_text("Invalid Jumia URL. Try again or cancel.")
        return

    category = context.user_data.get("add_category")
    if not category:
        await update.message.reply_text("Session expired. Start over.")
        context.user_data.clear()
        return

    # Fake args for add_watch
    context.args = [url]  # Add target/direction parsing if needed

    await add_watch(update, context)

    # Add category to last watch
    watches = user_watches.get(user_id, [])
    if watches:
        watches[-1]["category"] = category

    context.user_data.clear()
    await update.message.reply_text(f"Added in {category.capitalize()}! 🎉", reply_markup=build_main_menu(user_id))


def get_application_handlers():
    return [
        CommandHandler("start", start),
        CommandHandler("add", add_watch),
        CommandHandler("list", list_watches),
        CommandHandler("remove", remove_watch),
        CommandHandler("assign_trial", assign_trial),  # New admin command
        CallbackQueryHandler(inline_callback_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_url),
    ]