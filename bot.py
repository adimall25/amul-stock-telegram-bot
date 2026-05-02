"""
bot.py — Amul Stock Tracker Telegram Bot
-----------------------------------------
Commands:
  /start    — Register and set your delivery pincode
  /pincode  — Update your delivery pincode
  /products — Browse all products and manage your subscriptions
  /list     — View your active subscriptions with current stock status
  /status   — See live stock status for all products in your area
  /stop     — Unsubscribe and delete all your data
  /help     — Show all commands

Config (via .env):
  BOT_TOKEN   — Telegram bot token from @BotFather
  MONGODB_URI — MongoDB Atlas connection string
"""

import logging
import os

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN   = os.environ["BOT_TOKEN"]
MONGODB_URI = os.environ["MONGODB_URI"]
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── MongoDB ───────────────────────────────────────────────────────────────────
client    = MongoClient(MONGODB_URI)
db        = client["product_bot"]
users_col = db["users"]
prods_col = db["products"]
stock_col = db["stock"]
# ─────────────────────────────────────────────────────────────────────────────

AWAITING_PINCODE        = 1
AWAITING_PINCODE_UPDATE = 2


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_user(chat_id: str) -> dict | None:
    return users_col.find_one({"chat_id": chat_id})

def get_all_products() -> list[dict]:
    return list(prods_col.find())

def get_stock(product_id: str, pincode: str) -> dict | None:
    return stock_col.find_one({"product_id": product_id, "pincode": pincode})

def user_subscriptions(chat_id: str) -> list[str]:
    user = get_user(chat_id)
    return user.get("subscribed_products", []) if user else []

def is_valid_pincode(pincode: str) -> bool:
    return pincode.isdigit() and len(pincode) == 6

def stock_label(stock: dict | None) -> tuple[str, str]:
    """Returns (icon, text) for a stock entry."""
    if stock is None:
        return "⏳", "Not checked yet"
    if stock.get("available") is True:
        return "🟢", "In stock"
    if stock.get("available") is False:
        return "🔴", "Out of stock"
    return "⚠️", "Unknown"


# ── Keyboards ─────────────────────────────────────────────────────────────────

def build_products_keyboard(chat_id: str, products: list[dict], pincode: str) -> InlineKeyboardMarkup:
    subs = user_subscriptions(chat_id)
    subscribed_all = "all" in subs
    buttons = []

    for p in products:
        pid = str(p["_id"])
        is_subbed = subscribed_all or pid in subs
        stock = get_stock(pid, pincode)
        s_icon, _ = stock_label(stock)
        sub_icon = "✅" if is_subbed else "➕"
        buttons.append([InlineKeyboardButton(
            f"{sub_icon} {s_icon}  {p['name']}",
            callback_data=f"toggle:{pid}"
        )])

    buttons.append([
        InlineKeyboardButton("✅ Subscribe to all", callback_data="sub:all"),
        InlineKeyboardButton("🗑 Remove all",       callback_data="sub:none"),
    ])
    buttons.append([InlineKeyboardButton("🔄 Refresh stock status", callback_data="refresh:products")])
    return InlineKeyboardMarkup(buttons)


# ── /start ────────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)

    if get_user(chat_id):
        user = get_user(chat_id)
        await update.message.reply_text(
            f"Welcome back! 👋\n\n"
            f"📍 Your delivery pincode: *{user.get('pincode', 'not set')}*\n\n"
            f"/products — browse & subscribe to products\n"
            f"/status   — check live stock in your area\n"
            f"/list     — view your active alerts\n"
            f"/pincode  — update your delivery pincode\n"
            f"/help     — see all commands",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "👋 Welcome to *Amul Stock Tracker*!\n\n"
        "I monitor Amul product availability and alert you the moment something you care about is back in stock — "
        "so you're always first in line.\n\n"
        "📍 What's your *6-digit delivery pincode*?",
        parse_mode="Markdown",
    )
    return AWAITING_PINCODE


async def receive_pincode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    pincode = update.message.text.strip()

    if not is_valid_pincode(pincode):
        await update.message.reply_text(
            "That doesn't look like a valid pincode — it should be exactly 6 digits.\n\n"
            "Please try again:"
        )
        return AWAITING_PINCODE

    users_col.insert_one({
        "chat_id": chat_id,
        "pincode": pincode,
        "subscribed_products": [],
    })
    logger.info(f"New user registered: {chat_id}, pincode: {pincode}")

    await update.message.reply_text(
        f"✅ You're all set! Pincode *{pincode}* saved.\n\n"
        f"Use /products to pick which Amul products you want to track. "
        f"I'll alert you whenever their stock status changes in your area.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def start_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "No worries — registration cancelled.\n"
        "Send /start whenever you're ready."
    )
    return ConversationHandler.END


# ── /pincode ──────────────────────────────────────────────────────────────────

async def pincode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    user = get_user(chat_id)

    if not user:
        await update.message.reply_text(
            "You'll need to register first — send /start to get going."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        f"📍 Current delivery pincode: *{user['pincode']}*\n\n"
        f"Send your new 6-digit pincode to update it, or /cancel to keep it as is:",
        parse_mode="Markdown",
    )
    return AWAITING_PINCODE_UPDATE


async def receive_pincode_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = str(update.effective_chat.id)
    pincode = update.message.text.strip()

    if not is_valid_pincode(pincode):
        await update.message.reply_text(
            "That doesn't look right — pincodes are exactly 6 digits.\n\n"
            "Try again, or send /cancel to leave it unchanged:"
        )
        return AWAITING_PINCODE_UPDATE

    users_col.update_one({"chat_id": chat_id}, {"$set": {"pincode": pincode}})
    logger.info(f"User {chat_id} updated pincode → {pincode}")

    await update.message.reply_text(
        f"📍 Delivery pincode updated to *{pincode}*.\n\n"
        f"Your alerts will now reflect stock availability in this area.",
        parse_mode="Markdown",
    )
    return ConversationHandler.END


async def pincode_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Got it — your pincode is unchanged.")
    return ConversationHandler.END


# ── /products ─────────────────────────────────────────────────────────────────

async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user = get_user(chat_id)

    if not user:
        await update.message.reply_text(
            "You'll need to register first — send /start to get going."
        )
        return

    if not user.get("pincode"):
        await update.message.reply_text(
            "📍 No delivery pincode on file.\n\n"
            "Use /pincode to set one so I know which area to check stock for."
        )
        return

    products = get_all_products()
    if not products:
        await update.message.reply_text(
            "No products are being tracked yet — check back soon! 🥛"
        )
        return

    pincode = user["pincode"]

    # Product detail list above the keyboard
    prod_lines = []
    for i, p in enumerate(products, 1):
        s_icon, s_text = stock_label(get_stock(str(p["_id"]), pincode))
        line = f"{i}. *{p['name']}* — {s_icon} {s_text}"
        if p.get("description"):
            line += f"\n    _{p['description']}_"
        if p.get("url"):
            line += f"\n    🔗 {p['url']}"
        prod_lines.append(line)

    keyboard = build_products_keyboard(chat_id, products, pincode)
    await update.message.reply_text(
        f"📦 *Amul Products* — 📍 {pincode}\n\n"
        + "\n\n".join(prod_lines)
        + "\n\n"
        "Tap a product below to subscribe or unsubscribe:\n"
        "✅ = subscribed   ➕ = not subscribed\n"
        "🟢 = in stock   🔴 = out of stock   ⏳ = not checked yet",
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ── /list ─────────────────────────────────────────────────────────────────────

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user = get_user(chat_id)

    if not user:
        await update.message.reply_text(
            "You'll need to register first — send /start to get going."
        )
        return

    subs = user.get("subscribed_products", [])
    pincode = user.get("pincode", "")

    if not subs:
        await update.message.reply_text(
            "You don't have any active subscriptions yet.\n\n"
            "Use /products to browse what's available and subscribe to the ones you want to track."
        )
        return

    if "all" in subs:
        await update.message.reply_text(
            "📋 You're subscribed to *all products*.\n\n"
            "Use /products if you'd like to narrow it down.",
            parse_mode="Markdown",
        )
        return

    lines = [f"📋 *Your active alerts* — 📍 {pincode}\n"]
    for pid in subs:
        try:
            p = prods_col.find_one({"_id": ObjectId(pid)})
            if not p:
                continue
            s_icon, s_text = stock_label(get_stock(pid, pincode))
            line = f"{s_icon} *{p['name']}* — {s_text}"
            if p.get("url"):
                line += f"\n    🔗 {p['url']}"
            lines.append(line)
        except Exception:
            pass

    if len(lines) == 1:
        await update.message.reply_text(
            "None of your previously subscribed products exist anymore.\n\n"
            "Use /products to set up new subscriptions."
        )
        return

    lines.append("\n_Use /products to update your subscriptions._")
    await update.message.reply_text(
        "\n\n".join(lines),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


# ── /status ───────────────────────────────────────────────────────────────────

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)
    user = get_user(chat_id)

    if not user:
        await update.message.reply_text(
            "You'll need to register first — send /start to get going."
        )
        return

    pincode = user.get("pincode", "")
    products = get_all_products()

    if not products:
        await update.message.reply_text(
            "No products are being tracked yet — check back soon! 🥛"
        )
        return

    lines = [f"📊 *Live Stock Status* — 📍 {pincode}\n"]
    for p in products:
        pid = str(p["_id"])
        s_icon, s_text = stock_label(get_stock(pid, pincode))
        lines.append(f"{s_icon} *{p['name']}* — {s_text}")

    lines.append("\n_Stock is checked every hour._")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
    )


# ── /stop ─────────────────────────────────────────────────────────────────────

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = str(update.effective_chat.id)

    if not get_user(chat_id):
        await update.message.reply_text(
            "You're not currently registered — nothing to remove."
        )
        return

    users_col.delete_one({"chat_id": chat_id})
    await update.message.reply_text(
        "You've been unsubscribed and all your data has been deleted. 👋\n\n"
        "Changed your mind? Just send /start to register again."
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🥛 *Amul Stock Tracker*\n"
        "_Real-time Amul product availability alerts for your pincode._\n\n"
        "*Setup*\n"
        "/start   — Register and set your delivery pincode\n\n"
        "*Subscriptions*\n"
        "/products — Browse all products and toggle alerts\n"
        "/list     — See your current subscriptions and stock status\n"
        "/status   — Check live stock for all products in your area\n\n"
        "*Account*\n"
        "/pincode  — Update your delivery pincode\n"
        "/stop     — Unsubscribe and delete your data\n\n"
        "_Stock is checked every hour. You'll be notified whenever a product you're tracking becomes available or goes out of stock._",
        parse_mode="Markdown",
    )


# ── /cancel ───────────────────────────────────────────────────────────────────

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Cancelled — no changes made.")
    return ConversationHandler.END


# ── Callback query handler (inline button taps) ───────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    chat_id = str(query.from_user.id)
    data    = query.data
    user    = get_user(chat_id)

    if not user:
        await query.edit_message_text(
            "Your session has expired — send /start to register again."
        )
        return

    pincode  = user["pincode"]
    status_msg = None

    if data == "sub:all":
        users_col.update_one({"chat_id": chat_id}, {"$set": {"subscribed_products": ["all"]}})
        status_msg = "✅ Subscribed to all products — you'll be notified of any stock changes."

    elif data == "sub:none":
        users_col.update_one({"chat_id": chat_id}, {"$set": {"subscribed_products": []}})
        status_msg = "All subscriptions removed. Use /products to subscribe to individual products."

    elif data == "refresh:products":
        products = get_all_products()
        keyboard = build_products_keyboard(chat_id, products, pincode)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except Exception:
            pass
        return

    elif data.startswith("toggle:"):
        pid  = data.split(":", 1)[1]
        subs = user_subscriptions(chat_id)

        if "all" in subs:
            # Expand "all" into individual IDs, then remove just this one
            all_ids = [str(p["_id"]) for p in get_all_products()]
            new_subs = [i for i in all_ids if i != pid]
            users_col.update_one({"chat_id": chat_id}, {"$set": {"subscribed_products": new_subs}})
            status_msg = "Unsubscribed from that product. You're still tracking all others."
        elif pid in subs:
            users_col.update_one({"chat_id": chat_id}, {"$pull": {"subscribed_products": pid}})
            status_msg = "Alert removed — you won't be notified about that product anymore."
        else:
            users_col.update_one({"chat_id": chat_id}, {"$push": {"subscribed_products": pid}})
            status_msg = "✅ Subscribed! You'll get an alert as soon as this product is back in stock."
    else:
        return

    # Refresh the inline keyboard to reflect the new state
    products = get_all_products()
    keyboard = build_products_keyboard(chat_id, products, pincode)
    try:
        await query.edit_message_reply_markup(reply_markup=keyboard)
    except Exception:
        pass  # Safe to ignore — thrown when markup is identical to current

    if status_msg:
        await context.bot.send_message(chat_id=chat_id, text=status_msg)


# ── Unknown commands / messages ───────────────────────────────────────────────

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "I'm not sure what you mean by that.\n\n"
        "Use /help to see everything I can do."
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    start_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={AWAITING_PINCODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pincode)]},
        fallbacks=[CommandHandler("cancel", start_cancel)],
    )

    pincode_conv = ConversationHandler(
        entry_points=[CommandHandler("pincode", pincode_command)],
        states={AWAITING_PINCODE_UPDATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_pincode_update)]},
        fallbacks=[CommandHandler("cancel", pincode_cancel)],
    )

    app.add_handler(start_conv)
    app.add_handler(pincode_conv)
    app.add_handler(CommandHandler("products", products_command))
    app.add_handler(CommandHandler("list",     list_command))
    app.add_handler(CommandHandler("status",   status_command))
    app.add_handler(CommandHandler("stop",     stop))
    app.add_handler(CommandHandler("help",     help_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(MessageHandler(filters.TEXT,    unknown))

    logger.info("Bot is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
