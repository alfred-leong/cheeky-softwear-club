import asyncio
import os
import io
import base64
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

from dotenv import load_dotenv
from groq import Groq
from telegram import Update, InputMediaPhoto, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from sqlalchemy import create_engine, text as sql_text
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# Delay (seconds) to wait for all photos in a media group to arrive
MEDIA_GROUP_WAIT = 3.0
TIME_TO_CLEAR_DB = 12  # Time to clear the database (12 PM SGT)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- Example captions (stored in DB for persistence across deploys) ---

DEFAULT_CAPTIONS = [
    "Literally dead….. One of my favs i've ever listed!!! 💋 Gorgeous white embroidered floral jeans… SOOO baggy & flared!! Perfect mid-waisted length🤍🤍🤍 Such an it girl pair of jeans… might disappear 😝😝\n34.5cm waist max, 44cm hips, 25cm rise, 27cm thigh, 97cm\nlength, 27cm flare\n\nSB: $3",
    "This one's from glacier ⭐️⭐️⭐️ Gorgeous gorgeous details & touch of green paisleys on this… i luvvv 🧚‍♂️🧚\nFits XS-M; 37-57cm ptp, 57cm length\n\nSB: $3",
    "Cato pink striped halter ☀️💖Such a summery top i love!!! Can be worn as a tube too 💞💞\nFits XS-M, 37-47cm ptp, 42cm from pit\n\nSB: $3",
    "Such a flattering lace cami in the prettiest purple-grey colour! 💭💭 Love the horizontal lace detail\nat the bust ⭐️⭐️ Goes sooo well with your fav pair of jeans/shorts 🫶🫶\nFits XS-M; 38-48cm ptp, 57cm length\n\nSB: $3",
    "Camel Road low-waisted flared jeans 🦋🦋 Love the pop of white on the brand patch & the subtle navy embroidery at the back pockets 🩵 This one's for my lowkey girlieesss🫶🫶\n36-39cm across, 44cm hips max, 20cm rise, 26-30cm thigh, 100cm length\n\nSB: $3",
    "Gorgeous purple flowy chiffon cami 🫧🫧 Fits sooo pretty, i love the asymmetrical flowy bottom🤍🤍 Comes with a concealed side zip, bust is double lined💋💋\nFits XS-S, 37-40cm ptp, 66cm length\n\nSB: $3",
    "literally the most gorgeous yellow floral cami evaaaa 🌼🌼⭐️ So so pretty, i'm obsessed with the yellow florals & lace straps! Bust is double-lined😋\nFits XS-M, 35-50cm ptp, 64cm length\n\nSB: $3",
]

# --- Groq client ---
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

# --- Database ---
DB_URL = os.getenv("DB_URL")
engine = create_engine(DB_URL) if DB_URL else None


def _init_captions_table():
    """Create the example_captions table and seed defaults if empty."""
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("""
            CREATE TABLE IF NOT EXISTS example_captions (
                id SERIAL PRIMARY KEY,
                caption TEXT NOT NULL
            )
        """))
        count = conn.execute(sql_text("SELECT COUNT(*) FROM example_captions")).scalar()
        if count == 0:
            for cap in DEFAULT_CAPTIONS:
                conn.execute(sql_text("INSERT INTO example_captions (caption) VALUES (:cap)"), {"cap": cap})
            logger.info(f"Seeded {len(DEFAULT_CAPTIONS)} default captions into DB")


_init_captions_table()


def load_example_captions() -> list[str]:
    if engine is None:
        return list(DEFAULT_CAPTIONS)
    with engine.connect() as conn:
        rows = conn.execute(sql_text("SELECT caption FROM example_captions ORDER BY id")).fetchall()
    return [row.caption for row in rows]


def save_example_captions(captions: list[str]):
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM example_captions"))
        for cap in captions:
            conn.execute(sql_text("INSERT INTO example_captions (caption) VALUES (:cap)"), {"cap": cap})

# --- Conversation states ---
BATCH_COLLECTING, WAITING_DELETE_ID = range(2)


def build_prompt(example_captions: list[str], description: str | None = None) -> str:
    examples_block = "\n".join(
        f"  {i+1}) {cap}" for i, cap in enumerate(example_captions)
    )
    if description:
        desc_line = f'The seller has sent you photos of a clothing item along with this description:\n"{description}"'
    else:
        desc_line = "The seller has sent you photos of a clothing item. Describe what you see and write a caption for it."
    return f"""You are a caption writer for an online clothing store on social media.

{desc_line}

Here are example captions written by the seller — match this exact tone, energy, and emoji style:
{examples_block}

Rules:
- Write ONE caption for this clothing item.
- Match the seller's casual, enthusiastic, emoji-heavy style shown in the examples.
- Mention the brand / item name if given in the description.
- Keep it short (1-3 sentences max).
- Use relevant emojis liberally, matching the vibe of the examples.
- Do NOT add hashtags unless the examples use them.
- Always include measurements or sizing if given in the description. Place them on a new line right after the description text.
- End with a price line in the format "SB: $X" (X is a whole number). If no price is given in the description, default to $3. There must be two empty lines between the measurements/description and the "SB:" line.
- Output ONLY the caption text, nothing else."""


# =====================================================================
# Caption bot handlers (private chats)
# =====================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type

    if chat_type in ("group", "supergroup"):
        # Delete tracker: register chat in DB
        if engine is not None:
            with engine.begin() as conn:
                conn.execute(sql_text("""
                    INSERT INTO usernames (username, chat_id)
                    VALUES (:username, :chat_id)
                    ON CONFLICT (username) DO NOTHING
                """), {
                    "username": update.effective_chat.username,
                    "chat_id": update.effective_chat.id,
                })
        name = update.effective_chat.username or update.effective_chat.title
        await update.message.reply_text(
            f"Hi {name}, I will now store messages in the database. "
            f"Use /deleted in the discussion group to see deleted comments."
        )
    else:
        # Caption bot: show help
        await update.message.reply_text(
            "Hey! 👋 Send me an album of photos (2-4 pics) with a description "
            "in the caption and I'll generate a caption for you.\n\n"
            "Commands:\n"
            "/start - Show this help\n"
            "/batch - Batch mode (send multiple items, then /done)\n"
            "/addcaption <text> - Add an example caption\n"
            "/listcaptions - List saved example captions\n"
            "/deletecaption - Delete an example caption\n"
            "/cancel - Cancel current operation"
        )
        context.user_data.clear()

    return ConversationHandler.END


async def _finalize_single_item(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group_id: str):
    """Called after a delay once all photos in the album have arrived. Generates caption and sends."""
    await asyncio.sleep(MEDIA_GROUP_WAIT)
    user_data = context.application.user_data.get(chat_id, {})
    photos = user_data.pop("photos", [])
    description = user_data.pop("description", None)

    if not photos:
        return

    await context.bot.send_message(chat_id=chat_id, text="✨ Generating your caption...")
    example_captions = load_example_captions()
    await generate_and_send(chat_id, context.bot, photos, example_captions, description, context)


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect photos from an album, capture the caption, then auto-generate."""
    if "photos" not in context.user_data:
        context.user_data["photos"] = []

    # Get the highest resolution version of the photo
    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    context.user_data["photos"].append(buf.read())

    # Capture the caption/description from the album message
    if update.message.caption:
        context.user_data["description"] = update.message.caption

    media_group_id = update.message.media_group_id

    if media_group_id:
        # Part of an album — schedule generation after all photos arrive
        if "pending_group" not in context.user_data:
            context.user_data["pending_group"] = media_group_id
            asyncio.create_task(
                _finalize_single_item(context, update.effective_chat.id, media_group_id)
            )
    else:
        # Single photo — generate immediately
        photos = context.user_data.pop("photos", [])
        description = context.user_data.pop("description", None)
        await update.message.reply_text("✨ Generating your caption...")
        example_captions = load_example_captions()
        await generate_and_send(update.effective_chat.id, context.bot, photos, example_captions, description, context)


async def generate_and_send(chat_id: int, bot, photos: list[bytes],
                           example_captions: list[str],
                           description: str | None = None,
                           context: ContextTypes.DEFAULT_TYPE | None = None) -> str | None:
    """Generate a caption for photos and send them back as a forwardable media group.
    Returns the caption on success, or None on failure."""
    prompt = build_prompt(example_captions, description)

    # Build message content: images (base64) + text prompt
    content = []
    for photo_bytes in photos:
        b64 = base64.b64encode(photo_bytes).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    content.append({"type": "text", "text": prompt})

    try:
        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": content}],
        )
        caption = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Groq error: {e}")
        await bot.send_message(chat_id=chat_id,
                               text=f"Sorry, something went wrong generating the caption. Error: {e}")
        return None

    # Send photos back with caption as a forwardable media group
    media = []
    for i, photo_bytes in enumerate(photos):
        buf = io.BytesIO(photo_bytes)
        buf.name = f"photo_{i}.jpg"
        if i == 0:
            media.append(InputMediaPhoto(media=buf, caption=caption))
        else:
            media.append(InputMediaPhoto(media=buf))
    await bot.send_media_group(chat_id=chat_id, media=media)

    # Send a "Regenerate" button and store photos/description for reuse
    if context is not None:
        context.user_data["last_photos"] = photos
        context.user_data["last_description"] = description
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Regenerate", callback_data="regenerate")]
        ])
        await bot.send_message(chat_id=chat_id, text="Not happy? Tap to regenerate.", reply_markup=keyboard)

    return caption


async def regenerate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle the Regenerate button press."""
    query = update.callback_query
    await query.answer()

    photos = context.user_data.get("last_photos")
    description = context.user_data.get("last_description")

    if not photos:
        await query.edit_message_text("No photos to regenerate from. Send a new album!")
        return

    await query.edit_message_text("🔄 Regenerating...")
    example_captions = load_example_captions()
    await generate_and_send(update.effective_chat.id, context.bot, photos,
                           example_captions, description, context)


# =====================================================================
# Batch mode handlers
# =====================================================================

async def batch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enter batch mode — collect multiple clothing items."""
    context.user_data.clear()
    context.user_data["batch_items"] = []         # list of {"photos": [...], "description": ...}
    context.user_data["batch_group_map"] = {}     # media_group_id -> index
    context.user_data["pending_batch_groups"] = {}
    await update.message.reply_text(
        "📦 Batch mode! Send your clothing items as albums "
        "(2-4 pics per item, with a description in the caption).\n\n"
        "When you're done, send /done to generate captions."
    )
    return BATCH_COLLECTING


async def _batch_group_ack(context: ContextTypes.DEFAULT_TYPE, chat_id: int, media_group_id: str):
    """Delayed acknowledgment for a batch media group."""
    await asyncio.sleep(MEDIA_GROUP_WAIT)
    user_data = context.application.user_data.get(chat_id, {})
    items = user_data.get("batch_items", [])
    group_map = user_data.get("batch_group_map", {})
    idx = group_map.get(media_group_id)
    if idx is not None:
        count = len(items[idx]["photos"])
        total_items = len(items)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"📸 Item #{total_items} — got {count} photo{'s' if count != 1 else ''}. "
            f"Send more items or /done to generate.",
        )


async def batch_photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Collect photos in batch mode, grouping by media_group_id."""
    photo = update.message.photo[-1]
    file = await photo.get_file()
    buf = io.BytesIO()
    await file.download_to_memory(buf)
    buf.seek(0)
    photo_bytes = buf.read()

    items = context.user_data["batch_items"]
    group_map = context.user_data["batch_group_map"]
    media_group_id = update.message.media_group_id

    if media_group_id:
        if media_group_id not in group_map:
            group_map[media_group_id] = len(items)
            items.append({"photos": [], "description": None})
        item = items[group_map[media_group_id]]
        item["photos"].append(photo_bytes)
        if update.message.caption:
            item["description"] = update.message.caption

        # Debounced ack — one per album
        pending = context.user_data["pending_batch_groups"]
        pending[media_group_id] = pending.get(media_group_id, 0) + 1
        if pending[media_group_id] == 1:
            asyncio.create_task(
                _batch_group_ack(context, update.effective_chat.id, media_group_id)
            )
    else:
        # Single photo = its own item
        items.append({"photos": [photo_bytes], "description": update.message.caption})
        await update.message.reply_text(
            f"📸 Item #{len(items)} — got 1 photo. Send more items or /done to generate."
        )

    return BATCH_COLLECTING


async def batch_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process all collected items one by one."""
    items = context.user_data.get("batch_items", [])
    if not items:
        await update.message.reply_text("No photos received yet! Send some albums first.")
        return BATCH_COLLECTING

    total = len(items)
    await update.message.reply_text(f"⏳ Generating captions for {total} item{'s' if total != 1 else ''}...")

    example_captions = load_example_captions()

    for i, item in enumerate(items):
        await update.message.reply_text(f"✨ Processing item {i + 1}/{total}...")
        await generate_and_send(update.effective_chat.id, context.bot,
                                item["photos"], example_captions, item["description"], context)

    await update.message.reply_text(f"✅ All {total} items done!")
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Cancelled! Send /start to begin again.")
    return ConversationHandler.END


async def add_caption(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace("/addcaption", "", 1).strip()
    if not text:
        await update.message.reply_text("Usage: /addcaption <your example caption>")
        return
    captions = load_example_captions()
    captions.append(text)
    save_example_captions(captions)
    await update.message.reply_text(
        f"✅ Added! You now have {len(captions)} example captions."
    )


async def list_captions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captions = load_example_captions()
    if not captions:
        await update.message.reply_text("No example captions saved yet.")
        return
    lines = [f"{i+1}) {c}" for i, c in enumerate(captions)]
    await update.message.reply_text("📋 Example captions:\n\n" + "\n\n".join(lines))


async def delete_caption_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captions = load_example_captions()
    if not captions:
        await update.message.reply_text("No example captions to delete.")
        return ConversationHandler.END
    lines = [f"{i+1}) {c}" for i, c in enumerate(captions)]
    await update.message.reply_text(
        "📋 Example captions:\n\n" + "\n\n".join(lines)
        + "\n\nReply with the number of the caption to delete, or /cancel to abort."
    )
    return WAITING_DELETE_ID


async def delete_caption_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    captions = load_example_captions()
    try:
        idx = int(update.message.text.strip()) - 1
        if idx < 0 or idx >= len(captions):
            raise ValueError
    except ValueError:
        await update.message.reply_text(f"Please enter a number between 1 and {len(captions)}, or /cancel.")
        return WAITING_DELETE_ID

    removed = captions.pop(idx)
    save_example_captions(captions)
    preview = removed[:80] + "..." if len(removed) > 80 else removed
    await update.message.reply_text(
        f"🗑️ Deleted caption #{idx + 1}: {preview}\n\n"
        f"You now have {len(captions)} example caption{'s' if len(captions) != 1 else ''}."
    )
    return ConversationHandler.END


# =====================================================================
# Delete tracker handlers (group chats)
# =====================================================================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store reply messages in group chats for delete tracking."""
    if engine is None:
        return
    if not (update.message and update.message.reply_to_message):
        return

    message_id = update.message.message_id
    username = update.message.from_user.username or update.message.from_user.full_name
    text_msg = update.message.text or ""
    now = datetime.now()
    message_thread_id = update.message.message_thread_id
    message_chat_id = update.message.chat.id

    with engine.begin() as conn:
        result = conn.execute(sql_text("""
            INSERT INTO messages (id, message_id, username, text, timestamp, message_thread_id, chat_id)
            VALUES (gen_random_uuid(), :msg_id, :username, :text, :ts, :message_thread_id, :chat_id)
        """), {
            "msg_id": message_id,
            "username": username,
            "text": text_msg,
            "ts": now,
            "message_thread_id": message_thread_id,
            "chat_id": message_chat_id,
        })

        if result.rowcount == 1:
            logger.info("Insert into messages successful.")
        else:
            logger.warning("Insert into messages may have failed.")


async def handle_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store photo captions in group chats for delete tracking."""
    if engine is None:
        return
    if not update.message:
        return

    caption = update.message.caption
    if not caption:
        return

    message_id = update.message.message_id
    message_chat_id = update.message.chat.id

    with engine.begin() as conn:
        result = conn.execute(sql_text("""
            INSERT INTO items (message_id, caption, chat_id)
            VALUES (:msg_id, :caption, :chat_id)
        """), {
            "msg_id": message_id,
            "caption": caption,
            "chat_id": message_chat_id,
        })

        if result.rowcount == 1:
            logger.info("Insert into items successful.")
        else:
            logger.warning("Insert into items may have failed.")


async def show_deleted(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check which stored messages have been deleted and report them."""
    if engine is None:
        await update.message.reply_text("Database not configured.")
        return
    if not (update.message and update.message.from_user):
        return

    chat_id = update.effective_chat.id
    deleted = []

    with engine.connect() as conn:
        messages = conn.execute(sql_text("""
            SELECT message_id, username, text, message_thread_id
            FROM messages
            WHERE chat_id = :chat_id
        """), {"chat_id": chat_id}).fetchall()

        for msg in messages:
            try:
                forwarded_msg = await context.bot.forward_message(
                    chat_id=chat_id, from_chat_id=chat_id, message_id=msg.message_id
                )
                await context.bot.delete_message(
                    chat_id=chat_id, message_id=forwarded_msg.message_id
                )
            except Exception:
                with engine.connect() as inner_conn:
                    item_row = inner_conn.execute(sql_text("""
                        SELECT caption FROM items
                        WHERE message_id = :message_thread_id
                        AND chat_id = :chat_id
                    """), {
                        "message_thread_id": msg.message_thread_id,
                        "chat_id": chat_id,
                    }).fetchone()

                item_caption = item_row.caption if item_row else "(unknown item)"
                deleted.append(f"@{msg.username}: {msg.text}\nItem: {item_caption}\n\n")

    if not deleted:
        message = f"No deleted messages detected in '{update.effective_chat.title or chat_id}'."
    else:
        chat_label = update.effective_chat.title or f"Chat ID: {chat_id}"
        message = f"Deleted messages from {chat_label}:\n\n" + "\n".join(deleted)

    await context.bot.send_message(chat_id=chat_id, text=message)


async def clear_db():
    """Daily cleanup of messages and items tables."""
    if engine is None:
        return
    with engine.begin() as conn:
        conn.execute(sql_text("DELETE FROM messages"))
        conn.execute(sql_text("DELETE FROM items"))
    now = datetime.now().strftime("%Y-%m-%d")
    logger.info(f"Database cleared successfully at {TIME_TO_CLEAR_DB}PM on {now}")


# =====================================================================
# Application setup
# =====================================================================

async def post_init(application: Application):
    """Start the scheduler after the application initialises."""
    if engine is not None:
        scheduler = AsyncIOScheduler(timezone=timezone('Asia/Singapore'))
        scheduler.add_job(clear_db, trigger='cron', hour=TIME_TO_CLEAR_DB, minute=0)
        scheduler.start()
        logger.info("Scheduler started - DB cleanup at 12 PM SGT daily")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info(f"Health server listening on port {port}")


def main():
    start_health_server()
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addcaption", add_caption))
    app.add_handler(CommandHandler("listcaptions", list_captions))

    # Delete caption conversation
    delete_conv = ConversationHandler(
        entry_points=[CommandHandler("deletecaption", delete_caption_start)],
        states={
            WAITING_DELETE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, delete_caption_confirm),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(delete_conv)
    app.add_handler(CommandHandler("deleted", show_deleted))
    app.add_handler(CallbackQueryHandler(regenerate_callback, pattern="^regenerate$"))

    # Batch mode conversation (private chats only)
    batch_conv = ConversationHandler(
        entry_points=[
            CommandHandler("batch", batch_start),
        ],
        states={
            BATCH_COLLECTING: [
                MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, batch_photo_received),
                CommandHandler("done", batch_done),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),
        ],
    )
    app.add_handler(batch_conv)

    # Single-item caption generation (private chats only)
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, photo_received))

    # Delete tracker (group chats only)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.GROUPS, handle_item))

    logger.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
