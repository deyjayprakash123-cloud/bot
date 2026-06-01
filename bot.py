"""
BGMI Redeem Code Bot
====================
A Telegram bot that collects a user's BGMI ID and redeem code,
saves the order to SQLite, and forwards the details to an admin.

Environment Variables:
    BOT_TOKEN       - Telegram bot token from @BotFather
    ADMIN_CHAT_ID   - Telegram chat ID of the admin account
"""

import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------------------------------------------------------------------------
# Load environment variables from .env (local dev only; ignored on Render)
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN: str = os.environ["BOT_TOKEN"]          # raises KeyError if missing
ADMIN_CHAT_ID: int = int(os.environ["ADMIN_CHAT_ID"])

# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQLite database path
# ---------------------------------------------------------------------------
DB_PATH = "orders.db"

# ---------------------------------------------------------------------------
# ConversationHandler states
# ---------------------------------------------------------------------------
BGMI_ID, REDEEM_CODE = range(2)


# ===========================================================================
# Database helpers
# ===========================================================================

def init_db() -> None:
    """Create the orders table if it does not already exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id      TEXT PRIMARY KEY,
                telegram_id   INTEGER NOT NULL,
                username      TEXT,
                bgmi_id       TEXT NOT NULL,
                redeem_code   TEXT NOT NULL,
                submitted_at  TEXT NOT NULL
            )
            """
        )
        conn.commit()
    logger.info("Database initialised at '%s'.", DB_PATH)


def save_order(
    order_id: str,
    telegram_id: int,
    username: str | None,
    bgmi_id: str,
    redeem_code: str,
    submitted_at: str,
) -> None:
    """Insert a new order row into the database."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO orders
                (order_id, telegram_id, username, bgmi_id, redeem_code, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (order_id, telegram_id, username, bgmi_id, redeem_code, submitted_at),
        )
        conn.commit()
    logger.info("Order saved: %s", order_id)


def fetch_recent_orders(limit: int = 20) -> list[sqlite3.Row]:
    """Return the most recent *limit* orders, newest first."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            """
            SELECT order_id, telegram_id, username, bgmi_id, redeem_code, submitted_at
            FROM   orders
            ORDER  BY submitted_at DESC
            LIMIT  ?
            """,
            (limit,),
        )
        return cursor.fetchall()


# ===========================================================================
# Conversation handlers
# ===========================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point: send a welcome message and ask for the BGMI ID."""
    user = update.effective_user
    logger.info("User %s (%d) started the bot.", user.username, user.id)

    await update.message.reply_text(
        f"👋 Welcome, {user.first_name}!\n\n"
        "I help you submit BGMI redeem codes.\n\n"
        "📝 Please send me your *BGMI Player ID* to get started.",
        parse_mode="Markdown",
    )
    return BGMI_ID


async def receive_bgmi_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate and store the BGMI ID, then ask for the redeem code."""
    bgmi_id = update.message.text.strip()

    # --- Validation: reject empty or whitespace-only input ---
    if not bgmi_id:
        await update.message.reply_text(
            "⚠️ BGMI ID cannot be empty. Please send your *BGMI Player ID*.",
            parse_mode="Markdown",
        )
        return BGMI_ID  # stay in the same state

    # Store temporarily in user_data for this conversation
    context.user_data["bgmi_id"] = bgmi_id
    logger.info("BGMI ID received from user %d: %s", update.effective_user.id, bgmi_id)

    await update.message.reply_text(
        f"✅ Got it! BGMI ID: `{bgmi_id}`\n\n"
        "🎟️ Now please send me the *Redeem Code* you want to use.",
        parse_mode="Markdown",
    )
    return REDEEM_CODE


async def receive_redeem_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the redeem code, save the order, and notify the admin."""
    redeem_code = update.message.text.strip()

    # --- Validation: reject empty or whitespace-only input ---
    if not redeem_code:
        await update.message.reply_text(
            "⚠️ Redeem code cannot be empty. Please send a valid *Redeem Code*.",
            parse_mode="Markdown",
        )
        return REDEEM_CODE  # stay in the same state

    user = update.effective_user
    bgmi_id: str = context.user_data["bgmi_id"]
    order_id: str = str(uuid.uuid4())[:8].upper()   # short 8-char unique ID
    submitted_at: str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    username: str | None = user.username             # may be None if not set

    logger.info(
        "Redeem code received from user %d. Order ID: %s", user.id, order_id
    )

    # --- Persist to SQLite ---
    try:
        save_order(
            order_id=order_id,
            telegram_id=user.id,
            username=username,
            bgmi_id=bgmi_id,
            redeem_code=redeem_code,
            submitted_at=submitted_at,
        )
    except sqlite3.Error as exc:
        logger.error("Database error while saving order %s: %s", order_id, exc)
        await update.message.reply_text(
            "❌ An internal error occurred while saving your order. "
            "Please try again later or contact support."
        )
        return ConversationHandler.END

    # --- Notify admin ---
    admin_message = (
        "🆕 *New Order Received!*\n\n"
        f"🆔 Order ID  : `{order_id}`\n"
        f"👤 Username  : @{username or 'N/A'}\n"
        f"🔢 TG User ID: `{user.id}`\n"
        f"🎮 BGMI ID   : `{bgmi_id}`\n"
        f"🎟️ Redeem Code: `{redeem_code}`\n"
        f"🕒 Submitted : {submitted_at}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_message,
            parse_mode="Markdown",
        )
        logger.info("Admin notified for order %s.", order_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to notify admin for order %s: %s", order_id, exc)
        # Do NOT abort — the order is already saved; admin can use /orders.

    # --- Confirm to user ---
    await update.message.reply_text(
        f"🎉 *Thank you!* Your order has been submitted and will be processed shortly.\n\n"
        f"📦 Order ID: `{order_id}`\n"
        "We'll get back to you once the redeem code is applied. 🚀",
        parse_mode="Markdown",
    )

    # Clear temporary conversation data
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Allow the user to abort the conversation at any time with /cancel."""
    logger.info("User %d cancelled the conversation.", update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Order cancelled. Send /start whenever you want to try again."
    )
    return ConversationHandler.END


# ===========================================================================
# Admin command
# ===========================================================================

async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: display the 20 most recent orders."""
    user = update.effective_user

    # Only allow the designated admin to use this command
    if user.id != ADMIN_CHAT_ID:
        logger.warning(
            "Unauthorised /orders attempt by user %d (%s).", user.id, user.username
        )
        await update.message.reply_text("⛔ You are not authorised to use this command.")
        return

    rows = fetch_recent_orders(limit=20)

    if not rows:
        await update.message.reply_text("📭 No orders found yet.")
        return

    lines = ["📋 *Recent Orders (last 20)*\n"]
    for row in rows:
        lines.append(
            f"• `{row['order_id']}` | @{row['username'] or 'N/A'} | "
            f"BGMI: `{row['bgmi_id']}` | Code: `{row['redeem_code']}` | "
            f"{row['submitted_at']}"
        )

    # Telegram message length limit is 4096 chars; chunk if needed
    message = "\n".join(lines)
    if len(message) <= 4096:
        await update.message.reply_text(message, parse_mode="Markdown")
    else:
        # Send in chunks of 4000 chars
        for i in range(0, len(message), 4000):
            await update.message.reply_text(
                message[i : i + 4000], parse_mode="Markdown"
            )


# ===========================================================================
# Global error handler
# ===========================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions and optionally notify the admin."""
    logger.error("Unhandled exception:", exc_info=context.error)

    # Try to notify admin about the error
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Bot error:\n<code>{context.error}</code>",
            parse_mode="HTML",
        )
    except Exception:  # noqa: BLE001
        pass  # If admin notification fails, just log and move on


# ===========================================================================
# Application bootstrap
# ===========================================================================

def main() -> None:
    """Build and run the bot."""
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # ----- Conversation: collect BGMI ID → Redeem Code -----
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BGMI_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bgmi_id)
            ],
            REDEEM_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_redeem_code)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        # Persist conversation across bot restarts if you add persistence later
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_error_handler(error_handler)

    logger.info("Bot is starting — polling for updates...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
