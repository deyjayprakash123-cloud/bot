"""
BGMI Redeem Code Bot — Render Web Service Edition
==================================================
Runs as an HTTPS webhook server on Render.
Render provides PORT and RENDER_EXTERNAL_HOSTNAME automatically.

Environment Variables (set in Render dashboard)
------------------------------------------------
  BOT_TOKEN              – Telegram bot token from @BotFather  (REQUIRED)
  ADMIN_CHAT_ID          – Admin Telegram chat ID               (default: 8876073547)
  RENDER_EXTERNAL_HOSTNAME – Auto-set by Render (do NOT set manually)
  PORT                   – Auto-set by Render (do NOT set manually)

Deployment (Render Web Service)
--------------------------------
  Build : pip install -r requirements.txt
  Start : python bot.py
"""

import logging
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────────────────────────────────────────────────────────────────────
# Logging — stdout so Render streams it to the log panel immediately
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    stream=sys.stdout,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Configuration — read from environment (set in Render dashboard)
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv()  # no-op on Render; useful for local .env file

BOT_TOKEN: str = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logger.critical("BOT_TOKEN is not set. Set it in the Render environment variables.")
    sys.exit(1)

ADMIN_CHAT_ID: int = int(os.environ.get("ADMIN_CHAT_ID", "8876073547"))

# Render injects these two automatically for every Web Service
PORT: int = int(os.environ.get("PORT", 10000))
RENDER_HOST: str = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")

logger.info("Python %s", sys.version)
logger.info("Admin chat ID : %d", ADMIN_CHAT_ID)
logger.info("Listening on port: %d", PORT)
logger.info("Render host   : %s", RENDER_HOST or "(not set — running locally)")

# ──────────────────────────────────────────────────────────────────────────────
# SQLite — use /data (Render persistent disk) when available, else local dir
# ──────────────────────────────────────────────────────────────────────────────
_DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_PATH = os.path.join(_DATA_DIR, "orders.db")

# ──────────────────────────────────────────────────────────────────────────────
# ConversationHandler states
# ──────────────────────────────────────────────────────────────────────────────
BGMI_ID, REDEEM_CODE = range(2)


# ══════════════════════════════════════════════════════════════════════════════
# Database helpers
# ══════════════════════════════════════════════════════════════════════════════

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
    logger.info("Database ready at '%s'.", DB_PATH)


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


# ══════════════════════════════════════════════════════════════════════════════
# Conversation handlers
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Send welcome message and ask for BGMI Player ID."""
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
    """Validate the BGMI ID and ask for the redeem code."""
    bgmi_id = update.message.text.strip()

    if not bgmi_id:
        await update.message.reply_text(
            "⚠️ BGMI ID cannot be empty. Please send your *BGMI Player ID*.",
            parse_mode="Markdown",
        )
        return BGMI_ID

    context.user_data["bgmi_id"] = bgmi_id
    logger.info("BGMI ID from user %d: %s", update.effective_user.id, bgmi_id)

    await update.message.reply_text(
        f"✅ Got it! BGMI ID: `{bgmi_id}`\n\n"
        "🎟️ Now please send me the *Redeem Code* you want to use.",
        parse_mode="Markdown",
    )
    return REDEEM_CODE


async def receive_redeem_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Validate the redeem code, save the order, and notify admin."""
    redeem_code = update.message.text.strip()

    if not redeem_code:
        await update.message.reply_text(
            "⚠️ Redeem code cannot be empty. Please send a valid *Redeem Code*.",
            parse_mode="Markdown",
        )
        return REDEEM_CODE

    user         = update.effective_user
    bgmi_id      = context.user_data["bgmi_id"]
    order_id     = str(uuid.uuid4())[:8].upper()
    submitted_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    username     = user.username  # may be None

    logger.info("New order %s from user %d", order_id, user.id)

    # ── Persist to SQLite ────────────────────────────────────────────────────
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
        logger.error("DB error for order %s: %s", order_id, exc)
        await update.message.reply_text(
            "❌ Internal error while saving your order. Please try again later."
        )
        return ConversationHandler.END

    # ── Notify admin ─────────────────────────────────────────────────────────
    admin_msg = (
        "🆕 *New Order Received!*\n\n"
        f"🆔 Order ID    : `{order_id}`\n"
        f"👤 Username    : @{username or 'N/A'}\n"
        f"🔢 TG User ID  : `{user.id}`\n"
        f"🎮 BGMI ID     : `{bgmi_id}`\n"
        f"🎟️ Redeem Code  : `{redeem_code}`\n"
        f"🕒 Submitted   : {submitted_at}"
    )
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=admin_msg,
            parse_mode="Markdown",
        )
        logger.info("Admin notified for order %s.", order_id)
    except Exception as exc:
        logger.error("Failed to notify admin for order %s: %s", order_id, exc)

    # ── Confirm to user ───────────────────────────────────────────────────────
    await update.message.reply_text(
        "🎉 *Thank you!* Your order has been submitted and will be processed shortly.\n\n"
        f"📦 Order ID: `{order_id}`\n"
        "We'll get back to you once the redeem code is applied. 🚀",
        parse_mode="Markdown",
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Let the user abort the conversation with /cancel."""
    logger.info("User %d cancelled.", update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text(
        "❌ Order cancelled. Send /start whenever you want to try again."
    )
    return ConversationHandler.END


# ══════════════════════════════════════════════════════════════════════════════
# Admin command
# ══════════════════════════════════════════════════════════════════════════════

async def orders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: show the 20 most recent orders."""
    user = update.effective_user

    if user.id != ADMIN_CHAT_ID:
        logger.warning("Unauthorised /orders attempt by user %d.", user.id)
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

    message = "\n".join(lines)
    for i in range(0, len(message), 4000):
        await update.message.reply_text(message[i : i + 4000], parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════════════════
# Global error handler
# ══════════════════════════════════════════════════════════════════════════════

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every unhandled exception and ping the admin."""
    logger.error("Unhandled exception", exc_info=context.error)
    try:
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"⚠️ Bot error:\n<code>{context.error}</code>",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Application factory
# ══════════════════════════════════════════════════════════════════════════════

def build_app() -> Application:
    """Wire up all handlers and return the Application."""
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BGMI_ID:     [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_bgmi_id)],
            REDEEM_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_redeem_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("orders", orders_command))
    app.add_error_handler(error_handler)
    return app


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    init_db()
    app = build_app()

    if RENDER_HOST:
        # ── Webhook mode (Render Web Service) ────────────────────────────────
        # Render assigns a public HTTPS URL and injects PORT + RENDER_EXTERNAL_HOSTNAME.
        # We use the bot token as the URL path so random actors can't post fake updates.
        webhook_url = f"https://{RENDER_HOST}/{BOT_TOKEN}"
        logger.info("Starting in WEBHOOK mode: %s", webhook_url)

        app.run_webhook(
            listen="0.0.0.0",          # bind to all interfaces inside the container
            port=PORT,                  # Render's injected port (usually 10000)
            url_path=BOT_TOKEN,         # secret path — only Telegram knows it
            webhook_url=webhook_url,    # tells Telegram where to send updates
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # ── Polling mode (local development) ────────────────────────────────
        logger.info("RENDER_EXTERNAL_HOSTNAME not set — starting in POLLING mode.")
        app.run_polling(
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )


if __name__ == "__main__":
    main()
