import asyncio
import logging
import time
import urllib.parse
from typing import List, Dict, Optional
from telegram.ext import MessageHandler, filters
from telegram import Update
from telegram.ext import CommandHandler, ContextTypes

from bot.settings import (
    MAX_WATCHES_FREE,
    ADD_COOLDOWN_SECONDS,
    SCRAPE_TIMEOUT,
    ALLOWED_DIRECTIONS,
    CATEGORIES,
)
from utils.utils import scrape_product

# -------------------------
# In-memory store (MVP – replace with DB soon)
# -------------------------
user_watches: Dict[int, List[Dict]] = {}
_user_last_action: Dict[int, float] = {}
user_subscriptions = {}
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
        reply_markup=build_main_menu()
             return

    # New user → force type selection
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

def get_user_max_watches(user_id):
    sub = user_subscriptions.get(user_id, {})
    tier = sub.get("tier")
    
    if tier in PAID_TIERS:
        # Check if still in trial
        if "trial_start" in sub:
            days_since = (time.time() - sub["trial_start"]) / 86400
            if days_since <= PAID_TIERS[tier]["trial_days"]:
                return PAID_TIERS[tier]["max_watches"]  # Trial active → paid limit
        
        # Trial ended, but paid?
        if sub.get("paid", False):
            return PAID_TIERS[tier]["max_watches"]
    
    # Default free
    return DEFAULT_FREE_LIMIT

async def add_watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_settings:
    user_settings[user_id] = {
        "enabled_categories": CATEGORIES.copy(),  # all by default
        "min_change_percent": MIN_CHANGE_TO_ALERT,
    }
    max_allowed = get_user_max_watches(user_id)
    current_count = len(user_watches.get(user_id, []))
    
    if current_count >= max_allowed:
        tier_name = "Free" if max_allowed == DEFAULT_FREE_LIMIT else "your current plan"
        msg = (
            f"🚫 You've reached the limit ({max_allowed} watches) for {tier_name}.\n\n"
            "Upgrade to get more tracking capacity!"
        )
        keyboard = [[InlineKeyboardButton("See Upgrade Options", callback_data="upgrade_plans")]]
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
    if user_id not in user_settings:
    user_settings[user_id] = {
        "enabled_categories": CATEGORIES.copy(),  # all by default
        "min_change_percent": MIN_CHANGE_TO_ALERT,
        # ... other settings later
    }
    enabled = user_settings.get(user_id, {}).get("enabled_categories", [])
if enabled != CATEGORIES:
    text += f"\nAlert categories enabled: {', '.join(enabled).capitalize()}"
else:
    text += "\nAlerts for all categories"
    watches = user_watches.get(user_id, [])
    if not watches:
        await update.message.reply_text("📭 No watches. Add with /add <URL>")
        return

    lines = ["*Your Active Watches:*\n"]
    for i, w in enumerate(watches, 1):
        cat = w.get("category", "—").capitalize()
        title = w.get("title", "Unknown")
        url = w.get("url", "")
        price = safe_price_format(w.get("last_price"))
        target = f" | Target ≤ {safe_price_format(w.get('target_price'))}" if w.get("target_price") else ""
        mode_text = {"low": "drops only", "high": "increases", "both": "any change"}.get(w.get("direction", "low"), "drops only")
        lines.append(
            f"{i}. {title}{cat}\n"
            f"   Current: {price} | Target: {target} | Mode: {mode_text}\n"
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

def build_main_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Add New Watch", callback_data="add_watch_inline")],
        [InlineKeyboardButton("📋 My Watches", callback_data="my_watches_inline")],
        [InlineKeyboardButton("🔥 Hot Deals Channel", callback_data="hot_channel")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="dashboard_inline")],
        [InlineKeyboardButton("🔔 Alert Categories", callback_data="category_settings")],
        [InlineKeyboardButton("❓ Help", callback_data="help_inline")],
    ]
    return InlineKeyboardMarkup(keyboard)


def build_simple_back(target="main_menu_inline", label="↩️ Back"):
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=target)]])

def build_category_selection():
    keyboard = []
    for cat in CATEGORIES:
        # Make it look nice – you can add emojis per category later
        emoji = {
            'phones': '📱',
            'gadgets': '🎧',
            'laptops': '💻',
            'accessories': '🔌'
        }.get(cat, '📦')
        
        keyboard.append([
            InlineKeyboardButton(f"{emoji} {cat.capitalize()}", 
                               callback_data=f"cat_select_{cat}")
        ])
    
    keyboard.append([
        InlineKeyboardButton("↩️ Cancel", callback_data="add_watch_cancel")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def build_category_settings_menu(user_id):
    current_enabled = user_settings.get(user_id, {}).get("enabled_categories", CATEGORIES.copy())
    
    keyboard = []
    for cat in CATEGORIES:
        prefix = "✅ " if cat in current_enabled else "⬜ "
        keyboard.append([
            InlineKeyboardButton(f"{prefix}{cat.capitalize()}", 
                               callback_data=f"toggle_cat_{cat}")
        ])
    
    keyboard.append([InlineKeyboardButton("💾 Save & Exit", callback_data="save_categories")])
    keyboard.append([InlineKeyboardButton("↩️ Back to Main", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

async def inline_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # remove loading circle

    data = query.data

    try:
        if data in ("main_menu_inline", "back_to_main"):
            await query.edit_message_text(
                "🏠 **Naija Price Alerts Menu**\n\nWhat would you like to do?",
                parse_mode="Markdown",
                reply_markup=build_main_menu()
            )

        # ── Add Watch with Category ───────────────────────────────
        elif data == "add_watch_inline":
            await query.edit_message_text(
                "Great! First choose the **category** of the product:",
                reply_markup=build_category_selection()
            )

        elif data.startswith("cat_select_"):
            selected_cat = data.replace("cat_select_", "")
            
            if selected_cat not in CATEGORIES:
                await query.edit_message_text("Invalid category. Try again.")
                return

            # Store in user_data (temporary session storage)
            context.user_data["add_category"] = selected_cat
            
            await query.edit_message_text(
                f"Selected category: **{selected_cat.capitalize()}**\n\n"
                "Now send the **full Jumia product URL**:\n\n"
                "Example:\n`https://www.jumia.com.ng/samsung-galaxy-s24-ultra...`\n\n"
                "Or send /cancel to stop.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("↩️ Cancel", callback_data="add_watch_cancel")
                ]])
            )
            # Flag that next message should be treated as URL
            context.user_data["awaiting_add_url"] = True
        # In inline_callback_handler

elif data == "category_settings":
    await query.edit_message_text(
        "🔔 **Select categories for which you want to receive alerts**\n\n"
        "Toggle categories on/off (multiple allowed):",
        reply_markup=build_category_settings_menu(user_id)
    )

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
    
    # Refresh menu
    await query.edit_message_reply_markup(
        reply_markup=build_category_settings_menu(user_id)
    )

elif data == "save_categories":
    await query.edit_message_text(
        "✅ Category preferences saved!\n\nYou'll only receive alerts for selected categories.",
        reply_markup=build_main_menu()
    )
    
        elif data == "add_watch_cancel":
            context.user_data.pop("add_category", None)
            context.user_data.pop("awaiting_add_url", None)
            await query.edit_message_text(
                "Add watch cancelled.",
                reply_markup=build_main_menu()
            )
        elif data == "upgrade_plans":
    lines = ["💎 **Upgrade Options**\n\n"]
    for key, info in PAID_TIERS.items():
        trial = f"{info['trial_days']}-day free trial" if info['trial_days'] > 0 else ""
        lines.append(
            f"**{info['name']}** — ₦{info['price_monthly_ngn']:,}/month\n"
            f"• Max watches: {info['max_watches']}\n"
            f"• {trial}\n"
            f"• Features: {', '.join(info['features'])}\n\n"
        )
    lines.append("Contact @YourSupportHandle to start your free trial!")
    
    await query.edit_message_text("".join(lines), reply_markup=build_back_button())
    
        elif data == "my_watches_inline":
            await list_watches(update, context)  # reuse your existing function

        elif data == "hot_channel":
            await query.edit_message_text(
                "🔥 Join our **Hot Deals Channel** for the best public price drops!\n\n"
                "https://t.me/YourChannelNameHere",
                reply_markup=build_simple_back()
            )

        elif data == "dashboard_inline":
            watches_count = len(user_watches.get(update.effective_user.id, []))
            text = (
                f"👤 **Your Dashboard**\n\n"
                f"Active watches: **{watches_count}**\n"
                f"More stats coming soon..."
            )
            await query.edit_message_text(text, parse_mode="Markdown",
                                        reply_markup=build_simple_back())

        elif data == "help_inline":
            await query.edit_message_text(
                "❓ **Help**\n\n"
                "• /add <url> [target] — track product\n"
                "• /list — see your watches\n"
                "• /remove <number> — delete one\n\n"
                "Questions? Just message me!",
                parse_mode="Markdown",
                reply_markup=build_simple_back()
            )

        else:
            await query.edit_message_text("🤔 Unknown action.", reply_markup=build_main_menu())

    except Exception as e:
        logger.exception("Inline callback error")
        await query.message.reply_text("⚠️ Something went wrong... Try /start")

async def process_add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle URL when user is in 'add watch' flow"""
    if not context.user_data.get("awaiting_add_url"):
        return  # Not in add mode → ignore message

    url = update.message.text.strip()
    user_id = update.effective_user.id

    # Basic validation
    if not url.startswith(("http://", "https://")) or "jumia.com.ng" not in url.lower():
        await update.message.reply_text(
            "Please send a valid Jumia.ng product URL.\n\nTry again or /cancel",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Cancel", callback_data="add_watch_cancel")
            ]])
        )
        return

    category = context.user_data.get("add_category")
    if not category:
        await update.message.reply_text("Session expired. Please start over with Add Watch button.")
        context.user_data.clear()
        return

    # Now call your existing logic, but with category
    # We'll modify add_watch to accept optional parameters
    context.args = [url]  # fake args for existing function

    await add_watch(update, context)  # ← your original function

    # After success, store category in the watch
    watches = user_watches.get(user_id, [])
    if watches:
        # Last added watch = most recent
        last_watch = watches[-1]
        last_watch["category"] = category
        logger.info(f"Added category {category} to watch: {last_watch.get('title')}")

    # Clean up
    context.user_data.clear()

    await update.message.reply_text(
        f"Watch added successfully in **{category.capitalize()}** category! 🎉",
        reply_markup=build_main_menu()
    )

def get_application_handlers():
    return [
        CommandHandler("start", start),
        CommandHandler("add", add_watch),
        CommandHandler("list", list_watches),
        CommandHandler("remove", remove_watch),
        CallbackQueryHandler(inline_callback_handler),
        MessageHandler(filters.TEXT & ~filters.COMMAND, process_add_url),
    ]