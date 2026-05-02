"""
notify.py — Send Telegram alerts for stock changes
---------------------------------------------------
Reads the stock collection for availability flips and notifies
users whose pincode matches and who are subscribed to the product.

Run on a schedule via GitHub Actions (after scanner.py).

Config (environment variables or .env):
  BOT_TOKEN   — Telegram bot token
  MONGODB_URI — MongoDB Atlas connection string
"""

import asyncio
import logging
import os
from datetime import datetime, timezone

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from telegram import Bot
from telegram.constants import ParseMode

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN   = os.environ["BOT_TOKEN"]
MONGODB_URI = os.environ["MONGODB_URI"]
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
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


def get_product(product_id: str) -> dict | None:
    try:
        return prods_col.find_one({"_id": ObjectId(product_id)})
    except Exception:
        return None


def get_subscribers(product_id: str, pincode: str) -> list[dict]:
    """Users with matching pincode who are subscribed to this product."""
    return list(users_col.find({
        "pincode": pincode,
        "$or": [
            {"subscribed_products": "all"},
            {"subscribed_products": product_id},
        ],
    }))


def build_available_message(product: dict, pincode: str) -> str:
    lines = [
        f"🟢 *{product['name']}* is now available for delivery!",
        f"📍 Pincode: {pincode}",
    ]
    if product.get("description"):
        lines.append(f"_{product['description']}_")
    lines.append("")
    if product.get("url"):
        lines.append(f"🛒 Order here:\n{product['url']}")
    lines.append("\n_Use /status to see all products. Use /products to manage subscriptions._")
    return "\n".join(lines)


def build_unavailable_message(product: dict, pincode: str) -> str:
    lines = [
        f"🔴 *{product['name']}* is now out of stock.",
        f"📍 Pincode: {pincode}",
    ]
    if product.get("url"):
        lines.append(f"\n🔗 Product page:\n{product['url']}")
    lines.append("\n_You\'ll be notified when it\'s back in stock._")
    return "\n".join(lines)


def build_unavailable_message(product: dict, pincode: str) -> str:
    lines = [
        f"🔴 *{product['name']}* is now out of stock.",
        f"📍 Pincode: {pincode}\n",
        f"_You'll be notified when it's back in stock._",
        f"\n_Use /status to check all products._",
    ]
    return "\n".join(lines)


async def broadcast() -> None:
    bot = Bot(token=BOT_TOKEN)
    now = datetime.now(timezone.utc)
    total_sent = 0

    # ── Notify: became available ──────────────────────────────────────────────
    newly_available = list(stock_col.find({
        "available": True,
        "notified_available": False,
    }))
    logger.info(f"{len(newly_available)} product-pincode pair(s) newly available.")

    for entry in newly_available:
        product = get_product(entry["product_id"])
        if not product:
            continue

        pincode = entry["pincode"]
        subscribers = get_subscribers(entry["product_id"], pincode)
        message = build_available_message(product, pincode)
        sent = 0

        for user in subscribers:
            try:
                await bot.send_message(
                    chat_id=int(user["chat_id"]),
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False,
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Failed to send to {user['chat_id']}: {e}")

        stock_col.update_one(
            {"_id": entry["_id"]},
            {"$set": {"notified_available": True, "notified_at": now}},
        )
        logger.info(f"🟢 '{product['name']}' ({pincode}) — notified {sent}/{len(subscribers)} user(s).")
        total_sent += sent

    logger.info(f"Done. Total messages sent: {total_sent}")

    # ── Notify: became unavailable ────────────────────────────────────────────
    newly_unavailable = list(stock_col.find({
        "available": False,
        "notified_unavailable": False,
    }))
    logger.info(f"{len(newly_unavailable)} product-pincode pair(s) newly unavailable.")

    for entry in newly_unavailable:
        product = get_product(entry["product_id"])
        if not product:
            continue

        pincode = entry["pincode"]
        subscribers = get_subscribers(entry["product_id"], pincode)
        message = build_unavailable_message(product, pincode)
        sent = 0

        for user in subscribers:
            try:
                await bot.send_message(
                    chat_id=int(user["chat_id"]),
                    text=message,
                    parse_mode=ParseMode.MARKDOWN,
                )
                sent += 1
            except Exception as e:
                logger.warning(f"Failed to send to {user['chat_id']}: {e}")

        stock_col.update_one(
            {"_id": entry["_id"]},
            {"$set": {"notified_unavailable": True, "notified_at": now}},
        )
        logger.info(f"🔴 '{product['name']}' ({pincode}) — notified {sent}/{len(subscribers)} user(s).")
        total_sent += sent

    logger.info(f"Done. Total messages sent: {total_sent}")


def main():
    asyncio.run(broadcast())


if __name__ == "__main__":
    main()
